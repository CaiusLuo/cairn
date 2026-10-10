"""Read-only repository inventory with explicit bounds and filesystem checks."""

import hashlib
import os
import stat
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from cairn.repo_graph.graph import RepoGraph
from cairn.repo_graph.manifests import parse_manifest
from cairn.repo_graph.models import (
    BuildIssue,
    BuildLimits,
    BuildReport,
    DirectoryRecord,
    FileKind,
    FileRecord,
    FileStatus,
    ImportRecord,
    IssueReason,
    Language,
    LanguageRecord,
    ManifestKind,
    ManifestRecord,
    ManifestStatus,
    ModuleRecord,
    PythonStatus,
    SkipCount,
    SkipReason,
    SymbolRecord,
)
from cairn.repo_graph.python import parse_python, resolve_dependencies
from cairn.workspace.workspace import Workspace

DEFAULT_EXCLUSIONS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "target",
        "vendor",
        "coverage",
        ".next",
        ".nuxt",
        "generated",
        "out",
        "site-packages",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cairn",
        ".env",
    }
)

_MANIFESTS = {
    "pyproject.toml": ManifestKind.PYTHON,
    "package.json": ManifestKind.NODE,
    "pom.xml": ManifestKind.JAVA,
    "build.gradle": ManifestKind.GRADLE,
    "build.gradle.kts": ManifestKind.GRADLE,
    "go.mod": ManifestKind.GO,
    "Cargo.toml": ManifestKind.RUST,
}
_LANGUAGES = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".toml": Language.TOML,
    ".json": Language.JSON,
    ".xml": Language.XML,
    ".txt": Language.TEXT,
    ".md": Language.TEXT,
    ".rst": Language.TEXT,
    ".yaml": Language.TEXT,
    ".yml": Language.TEXT,
    ".ini": Language.TEXT,
    ".cfg": Language.TEXT,
    ".sh": Language.TEXT,
}
_SOURCE_LANGUAGES = frozenset(
    {
        Language.PYTHON,
        Language.JAVASCRIPT,
        Language.TYPESCRIPT,
        Language.JAVA,
        Language.KOTLIN,
        Language.GO,
        Language.RUST,
    }
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, result: os.stat_result) -> "_Identity":
        return cls(
            result.st_dev,
            result.st_ino,
            result.st_mode,
            result.st_size,
            result.st_mtime_ns,
            result.st_ctime_ns,
        )


class RepoGraphBuilder:
    """Build a bounded snapshot; only complete candidates become ``snapshot``.

    The root counts as one entry and has path ``.``. Detecting an overflowing
    directory can inspect one entry beyond ``max_entries``; its whole candidate
    listing is discarded. Symlinks have metadata records but are never opened.
    Supplied exclusions add to the mandatory defaults.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        limits: BuildLimits | None = None,
        exclusions: frozenset[str] = DEFAULT_EXCLUSIONS,
        python_roots: tuple[str, ...] = (".",),
    ) -> None:
        for name in exclusions:
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or "\x00" in name
            ):
                raise ValueError("exclusions must contain single entry basenames")
        if not isinstance(python_roots, tuple) or not 1 <= len(python_roots) <= 32:
            raise ValueError("python_roots must be a nonempty tuple of relative paths")
        for path in python_roots:
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 8192
                or "\x00" in path
                or "\\" in path
                or PurePosixPath(path).is_absolute()
                or any(part in {"..", ".git"} for part in path.split("/"))
                or PurePosixPath(path).as_posix() != path
            ):
                raise ValueError("python_roots must contain normalized relative paths")
        if len(set(python_roots)) != len(python_roots):
            raise ValueError("python_roots must be unique")
        self.workspace = workspace
        self.limits = limits or BuildLimits()
        self.exclusions = DEFAULT_EXCLUSIONS | exclusions
        self.python_roots = python_roots
        self.snapshot: RepoGraph | None = None
        self.last_report: BuildReport | None = None
        self._reset_inventory()

    def build(self) -> RepoGraph:
        self._reset_inventory()
        self._reset_structure()
        try:
            root_fd = self._open_root()
        except OSError:
            self._issue(IssueReason.UNREADABLE, ".")
        else:
            try:
                self._observed["."] = _Identity.from_stat(os.fstat(root_fd))
                self._walk(root_fd)
                self._verify(root_fd)
            finally:
                os.close(root_fd)
        report = self._make_report()
        graph = self._make_graph(report)
        self.last_report = report
        if report.complete:
            self.snapshot = graph
        return graph

    def _reset_inventory(self) -> None:
        self._files: dict[str, FileRecord] = {}
        self._directories = {".": DirectoryRecord(".")}
        self._manifests: dict[str, ManifestRecord] = {}
        self._observed: dict[str, _Identity] = {}
        self._listings: dict[str, tuple[str, ...]] = {}
        self._skips: Counter[SkipReason] = Counter()
        self._issues: dict[tuple[IssueReason, str], BuildIssue] = {}
        self._omitted_issues = 0
        self._complete = True
        self._entries_seen = 1
        self._bytes_read = 0
        self._entry_limit_reached = False

    def _reset_structure(self) -> None:
        self._modules: dict[str, ModuleRecord] = {}
        self._symbols: list[SymbolRecord] = []
        self._imports: list[ImportRecord] = []

    def _inspect_python(self, path: str, data: bytes, sha256: str) -> None:
        facts = parse_python(
            path,
            data,
            sha256,
            roots=self.python_roots,
            max_nodes=self.limits.max_ast_nodes,
            max_records=self.limits.max_structure_records
            - len(self._symbols)
            - len(self._imports),
        )
        self._modules[path] = facts.module
        self._symbols.extend(facts.symbols)
        self._imports.extend(facts.imports)
        if facts.module.status == PythonStatus.INVALID:
            self._issue(IssueReason.INVALID_PYTHON, path)
        elif facts.module.status == PythonStatus.LIMITED:
            self._issue(IssueReason.STRUCTURE_LIMIT, path)

    def _issue(self, reason: IssueReason, path: str) -> None:
        self._complete = False
        key = (reason, path)
        if key in self._issues:
            return
        if len(self._issues) < self.limits.max_issues:
            self._issues[key] = BuildIssue(reason, path)
        else:
            self._omitted_issues += 1

    def _changed(self, path: str) -> None:
        self._issue(IssueReason.CHANGED, path)
        record = self._files.get(path)
        if record is not None:
            self._files[path] = replace(
                record, status=FileStatus.CHANGED, source_sha256=None
            )
            self._manifests.pop(path, None)
            self._modules.pop(path, None)
            self._symbols = [record for record in self._symbols if record.path != path]
            self._imports = [record for record in self._imports if record.path != path]

    def _make_report(self) -> BuildReport:
        return BuildReport(
            complete=self._complete,
            entries_seen=self._entries_seen,
            bytes_read=self._bytes_read,
            skipped=tuple(
                SkipCount(reason, count)
                for reason, count in sorted(self._skips.items())
            ),
            issues=tuple(
                sorted(
                    self._issues.values(), key=lambda issue: (issue.path, issue.reason)
                )
            ),
            omitted_issues=self._omitted_issues,
        )

    def _make_graph(self, report: BuildReport) -> RepoGraph:
        counts = Counter(
            record.language
            for record in self._files.values()
            if record.status
            not in {FileStatus.SYMLINK, FileStatus.SPECIAL, FileStatus.BINARY}
        )
        return RepoGraph(
            root=self.workspace.root,
            files=tuple(self._files[path] for path in sorted(self._files)),
            directories=tuple(
                self._directories[path] for path in sorted(self._directories)
            ),
            languages=tuple(
                LanguageRecord(language, count)
                for language, count in sorted(counts.items())
            ),
            manifests=tuple(self._manifests[path] for path in sorted(self._manifests)),
            report=report,
            modules=tuple(self._modules[path] for path in sorted(self._modules)),
            symbols=tuple(
                sorted(
                    self._symbols,
                    key=lambda record: (record.path, record.line, record.name),
                )
            ),
            imports=tuple(
                sorted(
                    self._imports,
                    key=lambda record: (record.path, record.line, record.module or ""),
                )
            ),
            dependencies=resolve_dependencies(
                tuple(self._modules[path] for path in sorted(self._modules)),
                tuple(self._imports),
            ),
        )

    def _open_root(self) -> int:
        # Walk absolute parents too: O_NOFOLLOW on just the final root would
        # still follow a replaced ancestor symlink.
        fd = os.open(self.workspace.root.anchor, _DIRECTORY_FLAGS)
        try:
            for name in self.workspace.root.parts[1:]:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_directory(self, path: str, root_fd: int) -> int:
        fd = os.dup(root_fd)
        if path == ".":
            return fd
        walked: list[str] = []
        try:
            for name in path.split("/"):
                walked.append(name)
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child_fd
                if (
                    _Identity.from_stat(os.fstat(fd))
                    != self._observed["/".join(walked)]
                ):
                    raise OSError("directory changed during inspection")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _walk(self, root_fd: int) -> None:
        pending = [(".", 0)]
        while pending and not self._entry_limit_reached:
            path, depth = pending.pop()
            try:
                fd = self._open_directory(path, root_fd)
            except PermissionError:
                self._issue(IssueReason.UNREADABLE, path)
                continue
            except OSError:
                self._issue(IssueReason.CHANGED, path)
                continue
            try:
                children = self._scan(path, fd)
                if children is None:
                    continue
                subdirectories: list[tuple[str, int]] = []
                for name in children:
                    child_path = name if path == "." else f"{path}/{name}"
                    try:
                        identity = _Identity.from_stat(
                            os.stat(name, dir_fd=fd, follow_symlinks=False)
                        )
                    except OSError:
                        self._issue(IssueReason.UNREADABLE, child_path)
                        self._files[child_path] = FileRecord(
                            child_path,
                            self._language(child_path),
                            self._kind(child_path),
                            FileStatus.UNREADABLE,
                            0,
                            None,
                        )
                        continue
                    self._observed[child_path] = identity
                    if name in self.exclusions:
                        self._skips[SkipReason.EXCLUDED] += 1
                    elif stat.S_ISLNK(identity.mode):
                        self._skip_file(child_path, identity, SkipReason.SYMLINK)
                    elif stat.S_ISDIR(identity.mode):
                        self._directories[child_path] = DirectoryRecord(child_path)
                        if depth + 1 > self.limits.max_depth:
                            self._issue(IssueReason.DEPTH_LIMIT, child_path)
                        else:
                            subdirectories.append((child_path, depth + 1))
                    elif stat.S_ISREG(identity.mode):
                        self._read_file(child_path, name, identity, fd)
                    else:
                        self._skip_file(child_path, identity, SkipReason.SPECIAL)
                pending.extend(reversed(subdirectories))
            finally:
                os.close(fd)

    def _scan(self, path: str, fd: int) -> tuple[str, ...] | None:
        remaining = self.limits.max_entries - self._entries_seen
        names: list[str] = []
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > remaining:
                        self._entries_seen += len(names)
                        self._entry_limit_reached = True
                        self._issue(IssueReason.ENTRY_LIMIT, path)
                        return None
        except OSError:
            self._entries_seen += len(names)
            self._issue(IssueReason.UNREADABLE, path)
            return None
        self._entries_seen += len(names)
        names.sort()
        self._listings[path] = tuple(names)
        if _Identity.from_stat(os.fstat(fd)) != self._observed[path]:
            self._issue(IssueReason.CHANGED, path)
            return None
        return tuple(names)

    def _skip_file(self, path: str, identity: _Identity, reason: SkipReason) -> None:
        self._skips[reason] += 1
        self._files[path] = FileRecord(
            path,
            Language.UNKNOWN,
            FileKind.UNKNOWN,
            FileStatus.SYMLINK if reason == SkipReason.SYMLINK else FileStatus.SPECIAL,
            identity.size,
            None,
        )

    @staticmethod
    def _language(path: str) -> Language:
        return _LANGUAGES.get(Path(path).suffix.lower(), Language.UNKNOWN)

    @classmethod
    def _kind(cls, path: str) -> FileKind:
        if Path(path).name in _MANIFESTS:
            return FileKind.MANIFEST
        if cls._language(path) in _SOURCE_LANGUAGES:
            return FileKind.SOURCE
        return FileKind.TEXT

    def _read_file(
        self, path: str, name: str, identity: _Identity, parent_fd: int
    ) -> None:
        status = FileStatus.READABLE
        data: bytes | None = None
        if identity.size > self.limits.max_file_bytes:
            status = FileStatus.OVERSIZED
            self._issue(IssueReason.OVERSIZED, path)
        elif identity.size > self.limits.max_total_bytes - self._bytes_read:
            status = FileStatus.IO_LIMIT
            self._issue(IssueReason.IO_LIMIT, path)
        else:
            try:
                fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
            except OSError:
                try:
                    actual = _Identity.from_stat(
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    )
                except OSError:
                    actual = None
                if actual != identity:
                    status = FileStatus.CHANGED
                    self._issue(IssueReason.CHANGED, path)
                else:
                    status = FileStatus.UNREADABLE
                    self._issue(IssueReason.UNREADABLE, path)
            else:
                try:
                    if _Identity.from_stat(os.fstat(fd)) != identity:
                        status = FileStatus.CHANGED
                    else:
                        data = self._read_bytes(fd, identity.size)
                        if (
                            len(data) != identity.size
                            or _Identity.from_stat(os.fstat(fd)) != identity
                            or _Identity.from_stat(
                                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                            )
                            != identity
                        ):
                            status = FileStatus.CHANGED
                    if status == FileStatus.CHANGED:
                        self._issue(IssueReason.CHANGED, path)
                        data = None
                except OSError:
                    status = FileStatus.UNREADABLE
                    self._issue(IssueReason.UNREADABLE, path)
                    data = None
                finally:
                    os.close(fd)
        language = self._language(path)
        kind = self._kind(path)
        sha256 = None
        if data is not None:
            sha256 = hashlib.sha256(data).hexdigest()
            if self._is_binary(data, language):
                status = FileStatus.BINARY
                language = Language.UNKNOWN
                kind = FileKind.BINARY
                self._skips[SkipReason.BINARY] += 1
            if kind == FileKind.MANIFEST or Path(path).name in _MANIFESTS:
                manifest = parse_manifest(
                    path, data, limit=self.limits.max_manifest_items
                )
                self._manifests[path] = manifest
                if manifest.status == ManifestStatus.MALFORMED:
                    self._issue(IssueReason.MALFORMED_MANIFEST, path)
                elif manifest.status == ManifestStatus.LIMITED:
                    self._issue(IssueReason.MANIFEST_LIMIT, path)
                elif manifest.status == ManifestStatus.UNSUPPORTED:
                    self._issue(IssueReason.UNSUPPORTED_MANIFEST, path)
            if status == FileStatus.READABLE and language == Language.PYTHON:
                self._inspect_python(path, data, sha256)
        self._files[path] = FileRecord(
            path, language, kind, status, identity.size, sha256
        )

    def _read_bytes(self, fd: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                break
            self._bytes_read += len(chunk)
            remaining -= len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _is_binary(data: bytes, language: Language) -> bool:
        if b"\x00" in data:
            return True
        # Python's declared encoding is parsed from bytes by its AST inspector.
        if language == Language.PYTHON:
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    def _verify(self, root_fd: int) -> None:
        self._verify_root_path()
        for path, names in sorted(self._listings.items()):
            try:
                fd = self._open_directory(path, root_fd)
            except OSError:
                self._issue(IssueReason.CHANGED, path)
                continue
            try:
                if _Identity.from_stat(os.fstat(fd)) != self._observed[path]:
                    self._issue(IssueReason.CHANGED, path)
                actual_names: list[str] = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        actual_names.append(entry.name)
                        if len(actual_names) > len(names):
                            break
                if sorted(actual_names) != list(names):
                    self._issue(IssueReason.CHANGED, path)
                for name in names:
                    child_path = name if path == "." else f"{path}/{name}"
                    identity = self._observed.get(child_path)
                    if identity is None:
                        continue
                    try:
                        actual = _Identity.from_stat(
                            os.stat(name, dir_fd=fd, follow_symlinks=False)
                        )
                    except OSError:
                        self._changed(child_path)
                    else:
                        if actual != identity:
                            self._changed(child_path)
                if _Identity.from_stat(os.fstat(fd)) != self._observed[path]:
                    self._issue(IssueReason.CHANGED, path)
            except OSError:
                self._issue(IssueReason.CHANGED, path)
            finally:
                os.close(fd)
        self._verify_root_path()

    def _verify_root_path(self) -> None:
        try:
            path_fd = self._open_root()
        except OSError:
            self._issue(IssueReason.CHANGED, ".")
        else:
            try:
                if _Identity.from_stat(os.fstat(path_fd)) != self._observed["."]:
                    self._issue(IssueReason.CHANGED, ".")
            finally:
                os.close(path_fd)
