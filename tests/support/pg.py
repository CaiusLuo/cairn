"""Shared PostgreSQL databases for tests, isolated by namespace."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cairn.shared.db import Database

DEFAULT_TEST_DATABASE_URL = "postgresql:///cairn_test"
_CACHE: dict[tuple[str, str], Database] = {}


def test_database_url() -> str:
    return os.environ.get("CAIRN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def namespace_for(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return f"t_{digest}"


def database_for(root: Path) -> Database:
    """One migrated database per host path, unique to that path."""
    url = test_database_url()
    key = (url, namespace_for(root))
    database = _CACHE.get(key)
    if database is None:
        database = Database(url, namespace=key[1])
        database.migrate()
        _CACHE[key] = database
    return database


def reset_databases() -> None:
    while _CACHE:
        _, database = _CACHE.popitem()
        database.drop_schemas()
        database.close()
