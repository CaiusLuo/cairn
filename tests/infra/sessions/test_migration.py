"""Explicit host migration preserves legacy Sessions and consistent backups."""

import sqlite3
import stat
from pathlib import Path

import pytest

from cairn.infra.sessions.codec import restore_messages
from cairn.infra.sessions.models import SCHEMA_VERSION, SessionStoreError
from cairn.infra.sessions.store import SessionStore
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    binding,
    intent,
    legacy_store,
    messages,
    settled,
    store,
)
from tests.support.task_ledger import INPUT_DIGEST, TASK


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    # Use the actual historical DDL before writing any V1 records.
    api = legacy_store(tmp_path, version=1)
    try:
        bind = binding(tmp_path)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        api.commit_turn(revision, settled(turn))
    finally:
        api.close()
    return api.path


def assert_v1_backup(backup: Path, source: Path) -> None:
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with (
        sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True) as saved,
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as current,
    ):
        assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
        assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert saved.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            saved.execute(
                "SELECT name FROM sqlite_schema WHERE name='tasks'"
            ).fetchone()
            is None
        )
        for table in ("sessions", "turns", "checkpoints"):
            assert (
                saved.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                == current.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            )


def test_v1_read_and_session_writes_do_not_upgrade_implicitly(
    tmp_path: Path, legacy: Path
) -> None:
    directory = legacy.parent
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.schema_version == 1
        report = reader.inspect(SESSION)
        assert report.checkpoint.seq == 1
        with pytest.raises(SessionStoreError, match="schema_upgrade_required"):
            reader.inspect_task(SESSION, TASK)
    finally:
        reader.close()
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    writer = store(tmp_path)
    try:
        report = writer.inspect(SESSION)
        turn = intent(
            report.binding, report.revision, turn_id=OTHER_TURN, start=4, time=T4
        )
        revision = writer.begin_turn(report.revision, turn)
        receipt = writer.commit_turn(
            revision,
            settled(turn, history=restore_messages(report.checkpoint) + messages()),
        )
        assert receipt.revision.head_seq == 2 and writer.schema_version == 1
        with pytest.raises(SessionStoreError, match="schema_upgrade_required"):
            writer.create_task(
                receipt.revision, task_id=TASK, input_digest=INPUT_DIGEST
            )
        assert writer.schema_version == 1
    finally:
        writer.close()
    assert not list(directory.glob("schema-1-backup-*.sqlite3"))


def test_explicit_upgrade_preserves_committed_sessions_and_validated_backup(
    tmp_path: Path, legacy: Path
) -> None:
    api = store(tmp_path)
    try:
        original = api.inspect(SESSION)
        inode = legacy.stat().st_ino
        backup = api.upgrade_schema()
        assert api.schema_version == SCHEMA_VERSION and legacy.stat().st_ino == inode
        assert api.inspect(SESSION) == original
        assert_v1_backup(backup, legacy)
        task = api.create_task(
            original.revision, task_id=TASK, input_digest=INPUT_DIGEST
        )
        assert task.phase == "running" and api.inspect(SESSION) == original
        with pytest.raises(SessionStoreError, match="schema_upgrade_not_required"):
            api.upgrade_schema()
        assert len(list(legacy.parent.glob("schema-1-backup-*.sqlite3"))) == 1
    finally:
        api.close()


def test_backup_includes_committed_wal_pages(
    tmp_path: Path, legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = store(tmp_path)
    try:
        # Retain WAL deliberately; validation, transactions and backup stay real.
        monkeypatch.setattr(api, "_maintain", lambda: None)
        report = api.inspect(SESSION)
        turn = intent(
            report.binding, report.revision, turn_id=OTHER_TURN, start=4, time=T4
        )
        revision = api.begin_turn(report.revision, turn)
        receipt = api.commit_turn(
            revision,
            settled(turn, history=restore_messages(report.checkpoint) + messages()),
        )
        assert Path(str(legacy) + "-wal").stat().st_size > 0
        backup = api.upgrade_schema()
        assert_v1_backup(backup, legacy)
        assert api.inspect(SESSION).receipt == receipt
        with sqlite3.connect(backup) as conn:
            assert conn.execute("SELECT max(seq) FROM checkpoints").fetchone()[0] == 2
    finally:
        api.close()


@pytest.mark.parametrize("failure", ["backup", "ddl", "validation", "interrupt"])
def test_failed_upgrade_retains_original_database_and_committed_data(
    tmp_path: Path, legacy: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    api = store(tmp_path)
    original = api.inspect(SESSION)
    before = legacy.read_bytes()
    inode = legacy.stat().st_ino
    migrate = api._migrate_v1
    backup = api._backup_schema

    def fail_backup(conn: sqlite3.Connection) -> Path:
        backup(conn)
        raise OSError("credential-bearing-internal-diagnostic")

    def fail_migration(conn: sqlite3.Connection) -> None:
        migrate(conn)
        if failure == "validation":
            conn.execute("UPDATE checkpoints SET sha256=? WHERE seq=1", ("f" * 64,))
        elif failure == "interrupt":
            raise KeyboardInterrupt
        else:
            raise sqlite3.OperationalError("credential-bearing-internal-diagnostic")

    monkeypatch.setattr(api, "_backup_schema", fail_backup)
    if failure != "backup":
        monkeypatch.setattr(api, "_backup_schema", backup)
        monkeypatch.setattr(api, "_migrate_v1", fail_migration)
    try:
        if failure == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                api.upgrade_schema()
        else:
            with pytest.raises(SessionStoreError) as error:
                api.upgrade_schema()
            assert "credential" not in str(error.value)
        assert api.schema_version == 1
        retained = api.inspect(SESSION)
        assert retained.revision == original.revision
        assert retained.receipt == original.receipt
        assert retained.checkpoint == original.checkpoint
        assert legacy.stat().st_ino == inode and legacy.read_bytes() == before
        saved = list(legacy.parent.glob("schema-1-backup-*.sqlite3"))
        assert len(saved) == 1
        assert_v1_backup(saved[0], legacy)
    finally:
        api.close()
    reopened = store(tmp_path)
    try:
        assert reopened.schema_version == 1
        assert reopened.inspect(SESSION) == original
    finally:
        reopened.close()


@pytest.mark.parametrize("committed", [False, True])
def test_upgrade_commit_uncertainty_retains_v1_backup_and_reports_actual_schema(
    tmp_path: Path, legacy: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    api = store(tmp_path)
    original = api.inspect(SESSION)

    def uncertain(conn: sqlite3.Connection) -> None:
        if committed:
            conn.commit()
        raise sqlite3.OperationalError("sensitive-commit-error")

    monkeypatch.setattr(api, "_commit", uncertain)
    try:
        with pytest.raises(SessionStoreError, match="commit_uncertain"):
            api.upgrade_schema()
        assert api.schema_version == (SCHEMA_VERSION if committed else 1)
        assert api.inspect(SESSION).receipt == original.receipt
        backups = list(legacy.parent.glob("schema-1-backup-*.sqlite3"))
        assert len(backups) == 1
        assert_v1_backup(backups[0], legacy)
    finally:
        api.close()


def test_upgrade_requires_writer_ownership_and_no_live_turn(
    tmp_path: Path, legacy: Path
) -> None:
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="read_only_store"):
            reader.upgrade_schema()
    finally:
        reader.close()
    assert not list(legacy.parent.glob("schema-1-backup-*.sqlite3"))
    api = store(tmp_path)
    try:
        with pytest.raises(SessionStoreError, match="writer_busy"):
            store(tmp_path)
        report = api.inspect(SESSION)
        turn = intent(
            report.binding, report.revision, turn_id=OTHER_TURN, start=4, time=T4
        )
        revision = api.begin_turn(report.revision, turn)
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.upgrade_schema()
        assert api.schema_version == 1 and not api.inspect(SESSION).local_fault
        with pytest.raises(SessionStoreError, match="writer_busy"):
            store(tmp_path)
        api.commit_turn(
            revision,
            settled(turn, history=restore_messages(report.checkpoint) + messages()),
        )
        api.upgrade_schema()
        assert api.schema_version == SCHEMA_VERSION
    finally:
        api.close()


def test_corrupt_session_rejects_upgrade_before_backup_or_ddl(
    tmp_path: Path, legacy: Path
) -> None:
    with sqlite3.connect(legacy) as conn:
        conn.execute("UPDATE checkpoints SET sha256=? WHERE seq=1", ("e" * 64,))
    inode = legacy.stat().st_ino
    api: SessionStore | None = None
    try:
        api = store(tmp_path)
        # Ordinary writer admission can checkpoint WAL/configuration changes.
        # The rejected upgrade itself must leave the acquired database intact.
        before = legacy.read_bytes()
        with pytest.raises(SessionStoreError, match="digest_mismatch"):
            api.upgrade_schema()
        assert api.schema_version == 1
        assert legacy.read_bytes() == before and legacy.stat().st_ino == inode
    finally:
        if api is not None:
            api.close()
    assert legacy.read_bytes() == before and legacy.stat().st_ino == inode
    assert not list(legacy.parent.glob("schema-1-backup-*.sqlite3"))
