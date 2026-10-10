"""A snapshot describes one bounded inspection, never implicit live freshness."""

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from cairn.repo_graph.models import (
    BuildReport,
    DependencyRecord,
    DirectoryRecord,
    FileKind,
    FileRecord,
    ImportRecord,
    Language,
    LanguageRecord,
    ManifestRecord,
    ModuleRecord,
    PythonStatus,
    SymbolKind,
    SymbolRecord,
)

MAX_QUERY_ITEMS = 200
MAX_QUERY_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 8192
type _Record = FileRecord | SymbolRecord | ModuleRecord | DependencyRecord


@dataclass(frozen=True, slots=True)
class QueryResult[T]:
    items: tuple[T, ...]
    complete: bool
    truncated: bool
    unsupported_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptSummary:
    text: str
    complete: bool
    truncated: bool


def _limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_ITEMS}")


def _path(path: str, *, allow_root: bool = False) -> str:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 8192
        or "\x00" in path
        or "\\" in path
        or PurePosixPath(path).is_absolute()
        or any(part in {"..", ".git"} for part in path.split("/"))
        or PurePosixPath(path).as_posix() != path
        or (path == "." and not allow_root)
    ):
        raise ValueError("Expected a normalized Workspace-relative path")
    return path


def _enum(value: Enum | None, expected: type[Enum]) -> None:
    if value is not None and not isinstance(value, expected):
        raise ValueError("Invalid query filter")


def _size(record: _Record | str) -> int:
    value = record if isinstance(record, str) else asdict(record)
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class RepoGraph:
    root: Path
    files: tuple[FileRecord, ...]
    directories: tuple[DirectoryRecord, ...]
    languages: tuple[LanguageRecord, ...]
    manifests: tuple[ManifestRecord, ...]
    report: BuildReport
    modules: tuple[ModuleRecord, ...] = ()
    symbols: tuple[SymbolRecord, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    dependencies: tuple[DependencyRecord, ...] = ()

    def _query[T: (FileRecord, SymbolRecord, ModuleRecord, DependencyRecord)](
        self, records: tuple[T, ...], unsupported: tuple[str, ...], limit: int
    ) -> QueryResult[T]:
        _limit(limit)
        items: list[T] = []
        paths: list[str] = []
        used = 128  # Envelope and flags; the remaining budget bounds JSON facts.
        truncated = False
        for record in records:
            size = _size(record) + 1
            if len(items) == limit or used + size > MAX_QUERY_BYTES:
                truncated = True
                break
            items.append(record)
            used += size
        for path in unsupported:
            size = _size(path) + 1
            if len(paths) == limit or used + size > MAX_QUERY_BYTES:
                truncated = True
                break
            paths.append(path)
            used += size
        return QueryResult(
            tuple(items),
            self.report.complete and not truncated and not unsupported,
            truncated,
            tuple(paths),
        )

    def list_files(
        self,
        *,
        language: Language | None = None,
        kind: FileKind | None = None,
        path_prefix: str | None = None,
        limit: int = 20,
    ) -> QueryResult[FileRecord]:
        _limit(limit)
        _enum(language, Language)
        _enum(kind, FileKind)
        if path_prefix is not None:
            _path(path_prefix, allow_root=True)
        return self._query(
            tuple(
                record
                for record in self.files
                if (language is None or record.language == language)
                and (kind is None or record.kind == kind)
                and (
                    path_prefix in {None, "."}
                    or record.path == path_prefix
                    or record.path.startswith(f"{path_prefix}/")
                )
            ),
            (),
            limit,
        )

    def _unsupported_structure(self, path: str | None) -> tuple[str, ...]:
        parsed = {
            module.path
            for module in self.modules
            if module.status == PythonStatus.PARSED
        }
        return tuple(
            record.path
            for record in self.files
            if (path is None or record.path == path)
            and (
                record.kind == FileKind.SOURCE or record.path.endswith((".py", ".pyi"))
            )
            and record.path not in parsed
        )

    def find_symbols(
        self,
        *,
        name: str | None = None,
        kind: SymbolKind | None = None,
        path: str | None = None,
        limit: int = 20,
    ) -> QueryResult[SymbolRecord]:
        _limit(limit)
        _enum(kind, SymbolKind)
        if path is not None:
            _path(path)
        if name is not None and (
            not isinstance(name, str) or not name or len(name) > 256
        ):
            raise ValueError("Symbol name must contain 1 to 256 characters")
        return self._query(
            tuple(
                record
                for record in self.symbols
                if (name is None or record.name == name)
                and (kind is None or record.kind == kind)
                and (path is None or record.path == path)
            ),
            self._unsupported_structure(path),
            limit,
        )

    def module_for_path(self, path: str) -> QueryResult[ModuleRecord]:
        _path(path)
        records = tuple(module for module in self.modules if module.path == path)
        unsupported = self._unsupported_structure(path)
        if not records and any(record.path == path for record in self.files):
            unsupported = (path,)
        return self._query(records, unsupported, 1)

    def dependencies_of(
        self, path: str, *, limit: int = 20
    ) -> QueryResult[DependencyRecord]:
        _limit(limit)
        _path(path)
        unsupported = self._unsupported_structure(path)
        if not any(module.path == path for module in self.modules) and any(
            record.path == path for record in self.files
        ):
            unsupported = (path,)
        return self._query(
            tuple(record for record in self.dependencies if record.path == path),
            unsupported,
            limit,
        )

    def summary_for_prompt(
        self, *, limit: int = 20, max_bytes: int = 4096
    ) -> PromptSummary:
        _limit(limit)
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PROMPT_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MAX_PROMPT_BYTES}")
        files = self.list_files(limit=limit)
        symbols = self.find_symbols(limit=limit)
        state = "complete" if self.report.complete else "incomplete"
        lines = [
            f"Repository snapshot: {state}; freshness requires an explicit rebuild.",
            "Structure: Python AST only; other source languages unsupported.",
            f"Entries: {self.report.entries_seen}; bytes inspected: {self.report.bytes_read}.",
        ]
        lines.extend(
            f"File {json.dumps(record.path, ensure_ascii=True)}: "
            f"{record.language.value}, {record.status.value}"
            for record in files.items
        )
        lines.extend(
            f"{record.kind.value} {json.dumps(record.name, ensure_ascii=True)} at "
            f"{json.dumps(record.path, ensure_ascii=True)}:{record.line}"
            for record in symbols.items
        )
        text = "\n".join(lines)
        truncated = files.truncated or symbols.truncated or len(text) > max_bytes
        if truncated:
            marker = "\n[truncated]"
            text = (text[: max(0, max_bytes - len(marker))] + marker)[:max_bytes]
        return PromptSummary(
            text, self.report.complete and symbols.complete and not truncated, truncated
        )
