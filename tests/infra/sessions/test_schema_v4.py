"""Fences have one durable source and corrupted Turns cannot be blessed."""

import json
from pathlib import Path

import pytest

from cairn.agent.session.models import (
    SCHEMA_VERSION,
    SessionBinding,
    SessionStoreError,
)
from tests.support.sessions import SESSION, binding, fence, intent, settled, store


def test_v4_still_strips_fences_from_the_json_column(tmp_path: Path) -> None:
    """The table is the only fence source; the JSON must not keep a copy."""
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
        assert json.loads(raw).get("fences", []) == []
        rows = api._conn().execute("SELECT count(*) FROM tool_fences").fetchone()[0]
        assert rows == 1
    finally:
        api.close()


def test_a_corrupt_turn_cannot_be_fenced(tmp_path: Path) -> None:
    """The append fast path rejects what a full report rejects.

    `_prepare` used to rebuild the RecoveryReport here, which caught a Turn whose
    stored binding disagreed with the Session. Reading only the Turn must not
    accept it, or a fence would bless a corrupt record and the CAS would copy the
    corrupt binding onto the Session.
    """
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
            "UPDATE turns SET diagnostics_json=%s",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True),),
        )
        with pytest.raises(SessionStoreError, match="unresolved_turn_binding_mismatch"):
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
        with pytest.raises(SessionStoreError, match="unresolved_turn_not_active"):
            api.fence_tool(revision, fence(bind))
    finally:
        api._owned_turns.clear()
        api.close()


def _with_provider(bind: SessionBinding, provider: str) -> SessionBinding:
    from cairn.agent.session.models import ProviderReference

    reference = ProviderReference(
        provider=provider,
        model_group="group",
        model_ids=("model",),
        config_digest="0" * 64,
        system_prompt_digest="0" * 64,
        context_budget=100,
        output_reserve=10,
        max_steps=1,
    )
    return bind.model_copy(update={"provider": reference})


def test_a_provider_only_binding_drift_cannot_be_fenced(tmp_path: Path) -> None:
    """`same_workspace` ignores the provider; the append path must not.

    A Turn whose stored binding drifted only in provider still differs from the
    Session, and fencing it would let the CAS copy the forged binding onto the
    Session.
    """
    api = store(tmp_path)
    try:
        bind = _with_provider(binding(tmp_path), "trusted")
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        conn = api._conn()
        diagnostics = json.loads(
            conn.execute("SELECT diagnostics_json FROM turns").fetchone()[0]
        )
        diagnostics["binding"]["provider"]["provider"] = "forged"
        conn.execute(
            "UPDATE turns SET diagnostics_json=%s",
            (json.dumps(diagnostics, separators=(",", ":"), sort_keys=True),),
        )
        with pytest.raises(SessionStoreError, match="unresolved_turn_binding_mismatch"):
            api.fence_tool(revision, fence(bind))
    finally:
        api._owned_turns.clear()
        api.close()
