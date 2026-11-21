"""PostgreSQL access shared by every durable store.

Each store owns one schema inside a single database. Connections set a
domain-specific ``search_path`` so the stores' unqualified SQL keeps reading
naturally while table names stay unique across domains.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import Cursor
from psycopg import sql as psql
from psycopg_pool import ConnectionPool

DEFAULT_NAMESPACE = "cairn"
DOMAINS = ("core", "agent", "knowledge", "channel", "ops")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
_MIGRATION_0002_SQL = (
    Path(__file__).with_name("migrations") / "0002_knowledge_rag.sql"
).read_text(encoding="utf-8")
# Versioned migrations, applied in order. 0001 is the original schema.sql and
# contains non-idempotent ALTER TABLE statements, so later versions must be
# separate idempotent files rather than a re-run of the base schema.
_MIGRATIONS = (
    ("0001", _SCHEMA_SQL),
    ("0002", _MIGRATION_0002_SQL),
)
# A bounded wait mirrors the old database's busy timeout; a stuck lock is a
# storage failure, not an unbounded hang.
_CONNECT_OPTIONS = "-c lock_timeout=2000"


class Row(dict[str, Any]):
    """A row that supports both column-name and positional reads.

    The stores predate PostgreSQL and read ``row["name"]`` and ``row[0]``
    interchangeably; this keeps that code unchanged.
    """

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor: Cursor[Any]) -> Any:
    names = [column.name for column in cursor.description or ()]

    def build(values: tuple[Any, ...]) -> Row:
        return Row(zip(names, values, strict=True))

    return build


def _translate(sql: str) -> str:
    # The stores were written for sqlite3 placeholders; none of their SQL uses
    # PostgreSQL's jsonb ? operators.
    return sql.replace("?", "%s")


class _NoopCursor:
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


class DbConnection:
    """The subset of the old sqlite3 connection surface the stores used."""

    def __init__(self, raw: psycopg.Connection[Any]) -> None:
        self.raw = raw

    def execute(self, statement: str, parameters: Sequence[Any] | None = None) -> Any:
        stripped = statement.strip()
        keyword = stripped.upper()
        if keyword in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.raw.autocommit:
                return self.raw.execute("BEGIN")
            return _NoopCursor()
        if parameters is None:
            return self.raw.execute(_translate(stripped))
        return self.raw.execute(_translate(stripped), tuple(parameters))

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


class Database:
    """One PostgreSQL database with namespaced schemas for every store."""

    def __init__(
        self,
        url: str,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        pool_size: int = 5,
    ) -> None:
        if not _NAMESPACE.match(namespace):
            raise ValueError("database namespace must be lower-case snake_case")
        if not url or not url.strip():
            raise ValueError("database URL must not be empty")
        if pool_size < 1:
            raise ValueError("database pool size must be positive")
        self.url = url
        self.namespace = namespace
        self._pool = ConnectionPool(
            url,
            open=True,
            min_size=0,
            max_size=pool_size,
            kwargs={"row_factory": _row_factory, "options": _CONNECT_OPTIONS},
        )

    def schema(self, domain: str) -> str:
        if domain not in DOMAINS:
            raise ValueError(f"unknown database domain: {domain}")
        return f"{self.namespace}_{domain}"

    @property
    def schemas(self) -> tuple[str, ...]:
        return tuple(self.schema(domain) for domain in DOMAINS)

    def check(self) -> None:
        with self.connect("ops") as connection:
            connection.execute("SELECT 1")

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connect(
        self, domain: str, *, read_only: bool = False
    ) -> Iterator[DbConnection]:
        schema = self.schema(domain)
        with self._pool.connection() as raw:
            raw.execute(
                psql.SQL("SET search_path TO {}, public").format(
                    psql.Identifier(schema)
                )
            )
            if read_only:
                raw.execute("SET TRANSACTION READ ONLY")
            yield DbConnection(raw)

    def dedicated(self, domain: str) -> DbConnection:
        """A dedicated autocommit connection for callers that manage BEGIN."""
        schema = self.schema(domain)
        raw = psycopg.connect(
            self.url,
            autocommit=True,
            row_factory=_row_factory,
            options=_CONNECT_OPTIONS,
        )
        try:
            raw.execute(
                psql.SQL("SET search_path TO {}, public").format(
                    psql.Identifier(schema)
                )
            )
        except Exception:
            raw.close()
            raise
        return DbConnection(raw)

    @contextmanager
    def session(self, domain: str) -> Iterator[DbConnection]:
        connection = self.dedicated(domain)
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        """Create every store's tables and apply pending schema versions."""
        with self._pool.connection() as raw:
            for schema in self.schemas:
                raw.execute(
                    psql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        psql.Identifier(schema)
                    )
                )
        with self.connect("ops") as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = {
                str(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version, template in _MIGRATIONS:
                if version in applied:
                    continue
                rendered = template
                for domain in DOMAINS:
                    rendered = rendered.replace(
                        f"__{domain.upper()}__", self.schema(domain)
                    )
                for statement in rendered.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (version,),
                )

    def drop_schemas(self) -> None:
        """Remove this namespace; used by tests and by an explicit reset."""
        with self._pool.connection() as raw:
            for schema in self.schemas:
                raw.execute(
                    psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psql.Identifier(schema)
                    )
                )


__all__ = ["DOMAINS", "Database", "DbConnection", "Row"]
