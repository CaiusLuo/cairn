"""Explicit offline import: lock, back up, verify, publish; never alter originals."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

from cairn.agent.workspace.lease import WorkspaceLease
from cairn.development.catalogue import RuntimeCatalogue
from cairn.server.paths import ServerPaths, overlaps

COMPONENTS = ("runtime", "wechat", "schedule", "agent")


def _files(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Import refuses symlinks: {path}")
        if path.is_socket():
            continue
        if path.is_file():
            result.append(path)
        elif not path.is_dir():
            raise ValueError(f"Import refuses special files: {path}")
    return result


def _copy(source: Path, target: Path) -> None:
    target.mkdir(parents=True, mode=0o700)
    for path in _files(source):
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())


def _verify(root: Path, paths: ServerPaths) -> dict[str, str]:
    for file in _files(root):
        if file.suffix == ".json":
            try:
                json.loads(file.read_bytes())
            except (ValueError, UnicodeError):
                raise ValueError(f"Invalid JSON in import: {file.name}") from None
        if file.suffix == ".jsonl":
            try:
                for line in file.read_bytes().splitlines():
                    if line.strip():
                        json.loads(line)
            except (ValueError, UnicodeError):
                raise ValueError(f"Invalid JSONL in import: {file.name}") from None
        if file.suffix in {".sqlite3", ".sqlite", ".db"}:
            with closing(
                sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)
            ) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError(f"SQLite integrity check failed: {file.name}")
    catalogue = root / "runtime/runtime.json"
    if catalogue.exists():
        try:
            store = RuntimeCatalogue(
                catalogue.parent, forbidden_roots=(paths.workspace, paths.worktrees)
            )
            try:
                data = store.data
            finally:
                store.close()
        except ValueError:
            raise ValueError("Invalid imported Runtime catalogue") from None
        for task in data.tasks.values():
            if task.worktree and (
                Path(task.worktree).resolve() != Path(task.worktree)
                or not Path(task.worktree).is_relative_to(paths.worktrees)
                or not Path(task.worktree).is_dir()
            ):
                raise ValueError(
                    "Keep worktrees_root at the existing worktree location; import never rewrites ownership"
                )
    return {
        str(file.relative_to(root)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in _files(root)
    }


def import_state(
    paths: ServerPaths,
    source_root: Path,
    backup_root: Path,
    *,
    components: tuple[str, ...] = COMPONENTS,
    runtime_source: Path | None = None,
) -> Path:
    """Import selected old roots into empty targets. Keep Git worktrees in place."""
    for path in (source_root, backup_root, runtime_source):
        if path is not None and (not path.is_absolute() or path.is_symlink()):
            raise ValueError("Import paths must be absolute and must not be symlinks")
    source_root, backup_root = source_root.resolve(), backup_root.resolve()
    if (
        not components
        or len(set(components)) != len(components)
        or set(components) - set(COMPONENTS)
    ):
        raise ValueError("Select unique runtime/wechat/schedule/agent components")
    if (
        overlaps(source_root, paths.data_root)
        or overlaps(backup_root, source_root)
        or overlaps(backup_root, paths.data_root)
    ):
        raise ValueError("Source, destination and backup roots must be disjoint")
    for root in (source_root, backup_root):
        for writable in (paths.workspace, paths.worktrees):
            if root.is_relative_to(writable):
                raise ValueError("Import storage must be outside writable roots")
    sources = {name: source_root / name for name in components}
    if runtime_source is not None and "runtime" in components:
        sources["runtime"] = runtime_source.resolve()
    for source in sources.values():
        if source.is_symlink():
            raise ValueError("Legacy component root cannot be a symlink")
        if (
            overlaps(source, paths.data_root)
            or overlaps(source, backup_root)
            or any(source.is_relative_to(w) for w in (paths.workspace, paths.worktrees))
        ):
            raise ValueError(
                "Legacy component must be disjoint from destination, backup and writable roots"
            )
    if (paths.data_root / "state-import.json").exists():
        raise ValueError("Review the existing state-import.json before importing again")
    if not any(path.is_dir() for path in sources.values()):
        raise ValueError("No selected legacy state directories exist")
    destinations = {name: paths.data_root / name for name in components}
    for name, target in destinations.items():
        if target.exists() and any(target.iterdir()):
            raise ValueError(
                f"Import target is not empty: {name}; never merge live state"
            )
    with ExitStack() as locks:
        lock_paths = [source_root / "server.lock", paths.data_root / "server.lock"]
        for name, root in sources.items():
            if root.is_dir():
                # Stable lifetime locks also catch earlier versions without server.lock.
                lock_paths += [p for p in _files(root) if p.suffix == ".lock"]
                if name == "runtime":
                    lock_paths += [root / "owner.lock", root / "sessions/writer.lock"]
                if name == "wechat":
                    lock_paths += [root / "serve.lock"]
        for path in sorted(set(lock_paths)):
            if path.is_symlink():
                raise ValueError("Import lock cannot be a symlink")
            lease = WorkspaceLease(path)
            if not lease.acquire():
                raise RuntimeError(
                    f"State is in use; stop its owner explicitly before import: {path}"
                )
            locks.callback(lease.release)
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_root.chmod(0o700)
        backup = backup_root / ("import-" + uuid4().hex)
        backup.mkdir(mode=0o700)
        for name, source in sources.items():
            if source.exists():
                _copy(source, backup / name)
        manifest = _verify(backup, paths)
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (backup / "manifest.json").chmod(0o600)
        stage = Path(tempfile.mkdtemp(prefix=".state-import-", dir=paths.data_root))
        published: list[Path] = []
        try:
            for name in components:
                if (backup / name).exists():
                    _copy(backup / name, stage / name)
            if _verify(stage, paths) != manifest:
                raise ValueError("Imported files differ from the verified backup")
            for name, destination in destinations.items():
                if (stage / name).exists():
                    # Empty directories are harmless; occupied targets were rejected.
                    if destination.exists():
                        destination.rmdir()
                    os.rename(stage / name, destination)
                    published.append(destination)
            report = paths.data_root / "state-import.json"
            if report.exists():
                raise ValueError(
                    "An earlier import report exists; review it before another import"
                )
            report.write_text(
                json.dumps({"backup": str(backup), "files": manifest}, indent=2) + "\n"
            )
            report.chmod(0o600)
        except BaseException:
            for destination in published:
                shutil.rmtree(destination)
            raise
        finally:
            shutil.rmtree(stage)
        return backup
