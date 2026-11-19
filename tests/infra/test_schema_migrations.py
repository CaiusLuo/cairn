from pathlib import Path

import pytest

from cairn.shared import db as db_module
from cairn.shared.db import Database
from tests.support import pg


def _versions(database: Database) -> set[str]:
    with database.connect("ops") as connection:
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {str(row["version"]) for row in rows}


def _columns(database: Database, table: str) -> set[str]:
    with database.connect("knowledge") as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (database.schema("knowledge"), table),
        ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _tables(database: Database) -> set[str]:
    with database.connect("knowledge") as connection:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (database.schema("knowledge"),),
        ).fetchall()
    return {str(row["table_name"]) for row in rows}


def test_0002_upgrades_an_existing_0001_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(pg.test_database_url(), namespace=pg.namespace_for(tmp_path))
    original = db_module._MIGRATIONS
    database.drop_schemas()
    try:
        monkeypatch.setattr(db_module, "_MIGRATIONS", original[:1])
        database.migrate()
        assert _versions(database) == {"0001"}
        assert "status" not in _columns(database, "documents")
        assert "entities" not in _tables(database)

        monkeypatch.setattr(db_module, "_MIGRATIONS", original)
        database.migrate()
        assert _versions(database) == {"0001", "0002"}
        assert {
            "status",
            "error",
            "storage_path",
            "embedding_status",
            "graph_status",
            "attempts",
            "claimed_at",
            "updated_at",
            "processed_at",
        } <= _columns(database, "documents")
        assert {"entities", "chunk_entities", "relations"} <= _tables(database)

        # A repeat run must be a no-op rather than re-executing 0001.
        database.migrate()
        assert _versions(database) == {"0001", "0002"}
    finally:
        database.drop_schemas()
        database.close()
