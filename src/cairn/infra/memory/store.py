"""Bounded SQLite persistence. Mutations are owned by MemoryService, not tools.

Same-user arbitrary Python/SQL is trusted host code, not a security boundary.
The host must keep this directory outside ALL sandbox-writable roots.
"""

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import ValidationError

from cairn.core.memory.models import (
    MAX_ENTRIES,
    MAX_RECORD_BYTES,
    MemoryEntry,
    MemoryError,
    VersionConflict,
    check_sensitive,
)

SCHEMA_VERSION = 1
MAX_DATABASE_BYTES = 32 * 1024 * 1024


class MemoryStore:
    def __init__(
        self,
        directory: Path,
        project_id: str,
        *,
        forbidden_roots: tuple[Path, ...],
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        if len(project_id) != 64 or any(
            c not in "0123456789abcdef" for c in project_id
        ):
            raise MemoryError("Invalid memory project identity")
        self.project_id = project_id
        self.directory = directory.absolute() / project_id
        self.path = self.directory / "memory.sqlite3"
        self.sensitive_values = sensitive_values
        if not forbidden_roots or any(
            self.directory.resolve().is_relative_to(p.resolve())
            for p in forbidden_roots
        ):
            raise MemoryError("Memory store overlaps a tool-writable root")
        try:
            self._safe_paths()
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            self._initialize()
        except (OSError, sqlite3.Error):
            raise MemoryError("Memory storage unavailable") from None

    def _safe_paths(self) -> None:
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise MemoryError("Memory storage symlink rejected")
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.path) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                # SQLite may remove a WAL/shm between connections closing/opening.
                continue
            if stat.S_ISLNK(info.st_mode):
                raise MemoryError("Memory storage symlink rejected")
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DATABASE_BYTES * 2:
                raise MemoryError("Memory storage size or type invalid")

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            self._safe_paths()
            uri = self.path.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=2000")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA user_version").fetchone() != (
                SCHEMA_VERSION,
            ):
                raise MemoryError("Memory schema mismatch")
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise MemoryError("Memory database integrity failure")
            if connection.execute("SELECT project_id FROM metadata").fetchall() != [
                (self.project_id,)
            ]:
                raise MemoryError("Memory database project mismatch")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA max_page_count=8192")
                connection.execute("PRAGMA journal_size_limit=1048576")
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error, ValidationError, ValueError, TypeError):
            raise MemoryError(
                "Memory storage integrity or availability failure"
            ) from None
        finally:
            if connection is not None:
                connection.close()  # Rolls back incomplete transactions, including cancellation.

    def _initialize(self) -> None:
        # Exclusive creation refuses symlink redirection. Concurrent initializers serialize in SQL.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(
            sqlite3.connect(self.path, timeout=2, isolation_level=None)
        ) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA busy_timeout=2000")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise MemoryError("Memory schema mismatch")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if conn.execute("SELECT name FROM sqlite_master").fetchone():
                    raise MemoryError("Unrecognized memory database")
                conn.execute(
                    "CREATE TABLE entries (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
                conn.execute("CREATE TABLE metadata (project_id TEXT NOT NULL)")
                conn.execute("INSERT INTO metadata VALUES (?)", (self.project_id,))
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        with self._connect() as conn:
            if conn.execute("SELECT project_id FROM metadata").fetchall() != [
                (self.project_id,)
            ]:
                raise MemoryError("Memory database project mismatch")

    def _decode(self, value: str) -> MemoryEntry:
        if len(value.encode()) > MAX_RECORD_BYTES:
            raise MemoryError("Memory record limit exceeded")
        check_sensitive(value, self.sensitive_values)
        entry = MemoryEntry.model_validate_json(value)
        check_sensitive(entry.model_dump_json(), self.sensitive_values)
        if entry.project_id != self.project_id:
            raise MemoryError("Memory entry project mismatch")
        return entry

    def inspect(self, entry_id: str) -> MemoryEntry:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if row is None:
                raise MemoryError("Memory entry not found")
            return self._decode(row[0])

    def entries(self) -> tuple[MemoryEntry, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM entries ORDER BY id LIMIT ?", (MAX_ENTRIES + 1,)
            ).fetchall()
            if len(rows) > MAX_ENTRIES:
                raise MemoryError("Memory entry limit exceeded")
            return tuple(self._decode(row[0]) for row in rows)

    def _serialize(self, entry: MemoryEntry) -> str:
        # Revalidate even host-created model_copy/model_construct values before writing.
        try:
            text = entry.model_dump_json()
            checked = self._decode(text)
            check_sensitive(
                json.dumps(checked.model_dump(mode="json"), ensure_ascii=False),
                self.sensitive_values,
            )
            return text
        except (ValidationError, ValueError, TypeError):
            raise MemoryError("Invalid memory record") from None

    def _insert(self, entry: MemoryEntry) -> MemoryEntry:
        payload = self._serialize(entry)
        with self._connect(write=True) as conn:
            old = conn.execute(
                "SELECT payload FROM entries WHERE digest=?",
                (entry.candidate.digest(),),
            ).fetchone()
            if old is not None:
                return self._decode(
                    old[0]
                )  # Includes tombstones; never resurrect a proposal.
            if (
                conn.execute("SELECT count(*) FROM entries").fetchone()[0]
                >= MAX_ENTRIES
            ):
                raise MemoryError("Memory entry limit exceeded")
            conn.execute(
                "INSERT INTO entries VALUES (?,?,?,?)",
                (entry.id, entry.candidate.digest(), entry.version, payload),
            )
        return entry

    def _update(self, entries: tuple[tuple[MemoryEntry, int], ...]) -> None:
        payloads = [
            (entry, expected, self._serialize(entry)) for entry, expected in entries
        ]
        with self._connect(write=True) as conn:
            for entry, expected, payload in payloads:
                if entry.version != expected + 1:
                    raise VersionConflict()
                result = conn.execute(
                    "UPDATE entries SET version=?, payload=? WHERE id=? AND version=?",
                    (entry.version, payload, entry.id, expected),
                )
                if result.rowcount != 1:
                    raise VersionConflict()
