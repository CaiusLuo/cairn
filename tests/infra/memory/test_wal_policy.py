"""MemoryStore must qualify the SQLite runtime before touching its own data.

Project Memory writes WAL, so it inherits the same WAL-reset requirement as the
session store. These tests pin the admission contract from ADR 0002: refuse an
unqualified runtime by default, never inherit the session development
declaration, refuse before any filesystem effect, and leave existing databases
untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cairn.core.memory.models import MemoryError
from cairn.infra.memory.store import MemoryStore
from cairn.infra.sqlite_policy import (
    UNVERIFIED_SQLITE_DECLARATION,
    sqlite_wal_fix_supported,
    wal_admission,
)

PROJECT = "a" * 64


def _pin(monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]) -> None:
    """Present a chosen version to the real policy.

    Only the linked version is substituted; both policy functions compute their
    verdict from it, so these tests exercise the shipped rule rather than a copy.
    """
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)


@pytest.fixture(autouse=True)
def _clear_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UNVERIFIED_SQLITE_DECLARATION, raising=False)


def test_an_unqualified_runtime_is_refused_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusal precedes path creation: no directory, no database, no sidecar."""
    _pin(monkeypatch, (3, 40, 1))
    storage = tmp_path / "storage"

    with pytest.raises(MemoryError, match="runtime unqualified"):
        MemoryStore(storage, PROJECT, forbidden_roots=(tmp_path / "work",))

    assert not storage.exists()


def test_the_refusal_names_the_linked_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message reports the actually linked sqlite3, not a CLI version."""
    _pin(monkeypatch, (3, 50, 4))

    with pytest.raises(MemoryError) as refused:
        MemoryStore(tmp_path / "s", PROJECT, forbidden_roots=(tmp_path / "w",))

    assert "3.50.4" in str(refused.value)


def test_the_session_development_declaration_does_not_admit_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0002: one store's escape hatch must not authorise another's data."""
    _pin(monkeypatch, (3, 50, 4))
    monkeypatch.setenv(UNVERIFIED_SQLITE_DECLARATION, "1")

    with pytest.raises(MemoryError, match="runtime unqualified"):
        MemoryStore(tmp_path / "s", PROJECT, forbidden_roots=(tmp_path / "w",))


def test_a_qualified_runtime_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin(monkeypatch, (3, 53, 1))
    store = MemoryStore(tmp_path / "s", PROJECT, forbidden_roots=(tmp_path / "w",))

    assert store.wal_admission == "verified"
    assert store.path.is_file()


def test_the_gate_reads_the_shared_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """One version judgement: memory must not carry its own comparison."""
    _pin(monkeypatch, (3, 51, 3))
    assert sqlite_wal_fix_supported() is True
    assert wal_admission(honor_declaration=False) == "verified"
    _pin(monkeypatch, (3, 51, 2))
    assert sqlite_wal_fix_supported() is False
    assert wal_admission(honor_declaration=False) == "refused"


def test_existing_data_is_preserved_when_the_runtime_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused runtime must not rewrite, migrate or delete an existing database."""
    storage = tmp_path / "s"
    _pin(monkeypatch, (3, 53, 1))
    MemoryStore(storage, PROJECT, forbidden_roots=(tmp_path / "w",))

    database = storage / PROJECT / "memory.sqlite3"
    before = database.read_bytes()
    sidecars = sorted(p.name for p in database.parent.iterdir())

    _pin(monkeypatch, (3, 40, 1))
    with pytest.raises(MemoryError, match="runtime unqualified"):
        MemoryStore(storage, PROJECT, forbidden_roots=(tmp_path / "w",))

    assert database.read_bytes() == before
    assert sorted(p.name for p in database.parent.iterdir()) == sidecars
