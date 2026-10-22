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
