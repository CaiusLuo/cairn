"""Schema V4 keeps fences as the single durable evidence for a Turn."""

import sqlite3
from pathlib import Path

import pytest

from cairn.infra.sessions.models import (
    SCHEMA_VERSION,
    SessionStoreError,
)
from tests.support.sessions import (
    SESSION,
    binding,
    fence,
    intent,
    legacy_store,
    settled,
    store,
)


def _downgrade_to_v3(path: Path, turn_id: str) -> None:
    """Turn a V4 database into a genuine V3 one: fences back in JSON, no table."""
    import json

    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT diagnostics_json FROM turns").fetchone()
        diagnostics = json.loads(row[0])
        diagnostics["fences"] = [
            {
                "session_id": SESSION,
                "turn_id": turn_id,
                "ordinal": 1,
                "assistant_message_index": 1,
                "call_id": "call-1",
                "tool_name": "read_file",
                "observed_at_utc": "2020-01-01T00:00:02Z",
            }
        ]
        conn.execute(
            "UPDATE turns SET diagnostics_json=?",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True),),
        )
        conn.execute("DROP TABLE tool_fences")
        conn.execute("PRAGMA user_version=3")


def test_a_v3_writer_never_drops_a_turns_fences(tmp_path: Path) -> None:
    """A legacy schema holds its fences inline; a recovery write must keep them.

    Schema V4 strips fences from `diagnostics_json` because the table is their
    source of truth. On a V3 database the table does not exist and the JSON is
    the only copy, so stripping would delete the evidence a reconcile exists to
    preserve.
    """
    api = store(tmp_path)
    bind = binding(tmp_path)
    revision = api.create(bind).revision
    turn = intent(bind, revision)
    revision = api.begin_turn(revision, turn)
    revision = api.fence_tool(revision, fence(bind))
    # A crash removes nothing; the fence is committed evidence of a possible
    # effect and the Turn stays unresolved.
    api._owned_turns.clear()
    api.close()
    _downgrade_to_v3(api.path, turn.turn_id)

    legacy = store(tmp_path)
    assert legacy.schema_version == 3
    report = legacy.inspect(SESSION)
    assert len(report.attempts[0].diagnostics.fences) == 1
    # `require_recovery` is the recovery write that does not demand the Turn be
    # locally released first; it must preserve the inline fences on a V3 schema.
    report = legacy.inspect(SESSION)
    legacy.require_recovery(report.revision, "storage_failed")
    after = legacy.inspect(SESSION)
    assert after.status == "recovery_required"
    assert len(after.attempts[0].diagnostics.fences) == 1
    assert legacy._has_fence_table(legacy._conn()) is False
    legacy.close()


def test_v4_still_strips_fences_from_the_json_column(tmp_path: Path) -> None:
    """At V4 the table is the only fence source; the JSON must not keep a copy."""
    api = store(tmp_path)
    try:
        bind = binding(tmp_path)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        revision = api.fence_tool(revision, fence(bind))
        api.commit_turn(revision, settled(turn))
        assert api.schema_version == SCHEMA_VERSION
        assert len(api.inspect(SESSION).attempts[0].diagnostics.fences) == 1
        raw = api._conn().execute("SELECT diagnostics_json FROM turns").fetchone()[0]
        import json

        assert json.loads(raw).get("fences", []) == []
        rows = api._conn().execute("SELECT count(*) FROM tool_fences").fetchone()[0]
        assert rows == 1
    finally:
        api.close()


def test_a_legacy_schema_cannot_fence_before_upgrade(tmp_path: Path) -> None:
    """A V3 store has no fence table, so it must be upgraded before fencing."""
    api = legacy_store(tmp_path, version=3)
    try:
        bind = binding(tmp_path)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        with pytest.raises(SessionStoreError, match="schema_upgrade_required"):
            api.fence_tool(revision, fence(bind))
    finally:
        # The refused fence latches the store and leaves the Turn owned.
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.close()


def test_a_corrupt_turn_cannot_be_fenced(tmp_path: Path) -> None:
    """The append fast path rejects what a full report rejects.

    `_prepare` used to rebuild the RecoveryReport here, which caught a Turn whose
    stored binding disagreed with the Session. Reading only the Turn must not
    accept it, or a fence would bless a corrupt record and the CAS would copy the
    corrupt binding onto the Session.
    """
    import json

    api = store(tmp_path)
    try:
        bind = binding(tmp_path)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        conn = api._conn()
        diagnostics = json.loads(
            conn.execute("SELECT diagnostics_json FROM turns").fetchone()[0]
        )
        diagnostics["binding"]["workspace"]["root"]["path"] = "/somewhere/else"
        diagnostics["binding"]["workspace"]["root"]["inode"] = 999_999
        conn.execute(
            "UPDATE turns SET diagnostics_json=?",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True),),
        )
        conn.commit()
        with pytest.raises(SessionStoreError, match="record_binding_mismatch"):
            api.fence_tool(revision, fence(bind))
    finally:
        api._owned_turns.clear()
        api.close()


def test_a_session_without_its_active_turn_cannot_be_fenced(tmp_path: Path) -> None:
    """A Session that no longer names the executing Turn must reject a fence."""
    api = store(tmp_path)
    try:
        bind = binding(tmp_path)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        conn = api._conn()
        conn.execute("UPDATE sessions SET state='ready', active_turn_id=NULL")
        conn.commit()
        with pytest.raises(SessionStoreError, match="unresolved_turn_not_active"):
            api.fence_tool(revision, fence(bind))
    finally:
        api._owned_turns.clear()
        api.close()


def test_migration_moves_every_fence_and_keeps_the_report(tmp_path: Path) -> None:
    """A genuine V3 database upgrades to V4 without changing logical state."""
    api = store(tmp_path)
    bind = binding(tmp_path)
    revision = api.create(bind).revision
    turn = intent(bind, revision)
    revision = api.begin_turn(revision, turn)
    revision = api.fence_tool(revision, fence(bind))
    api.commit_turn(revision, settled(turn))
    api.close()
    _downgrade_to_v3(api.path, turn.turn_id)

    legacy = store(tmp_path)
    before = legacy.inspect(SESSION)
    assert legacy.schema_version == 3
    assert len(before.attempts[0].diagnostics.fences) == 1
    backup = legacy.upgrade_schema()
    after = legacy.inspect(SESSION)
    assert legacy.schema_version == SCHEMA_VERSION
    assert backup.name.startswith("schema-3-backup-")
    # Logical content is identical; only the storage shape moved.
    assert after.receipt == before.receipt
    assert after.revision == before.revision
    assert after.attempts[0].diagnostics.fences == before.attempts[0].diagnostics.fences
    rows = (
        legacy._conn()
        .execute("SELECT ordinal, call_id FROM tool_fences ORDER BY ordinal")
        .fetchall()
    )
    assert [tuple(row) for row in rows] == [(1, "call-1")]
    legacy.close()


def test_migration_failure_leaves_the_v3_database_intact(tmp_path: Path) -> None:
    """A fault during the V3->V4 step must not partially migrate the database."""
    api = store(tmp_path)
    bind = binding(tmp_path)
    revision = api.create(bind).revision
    turn = intent(bind, revision)
    revision = api.begin_turn(revision, turn)
    revision = api.fence_tool(revision, fence(bind))
    api.commit_turn(revision, settled(turn))
    api.close()
    _downgrade_to_v3(api.path, turn.turn_id)

    legacy = store(tmp_path)
    assert legacy.inspect(SESSION).attempts[0].diagnostics.fences

    def fail(conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("migration interrupted")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(legacy), "_migrate_v3", fail)
        with pytest.raises(SessionStoreError):
            legacy.upgrade_schema()
    legacy.close()

    # A fresh read-only inspector sees the untouched V3 database.
    reader = store(tmp_path)
    try:
        assert reader.schema_version == 3
        assert len(reader.inspect(SESSION).attempts[0].diagnostics.fences) == 1
        assert reader._has_fence_table(reader._conn()) is False
    finally:
        reader.close()
