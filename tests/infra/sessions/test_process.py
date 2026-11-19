"""SIGKILL tests application crashes, not disk/power-loss durability."""

from pathlib import Path

import pytest

from cairn.agent.session.models import HostResolution, SessionStoreError
from tests.support.sessions import (
    SESSION,
    T4,
    binding,
    crash_at,
    fence,
    store,
)


@pytest.mark.parametrize(
    "window",
    [
        "before_intent",
        "intent",
        "fence",
        "checkpoint_insert",
        "attempt_update",
        "before_commit",
        "committed",
    ],
)
def test_crash_barriers_restore_old_or_complete_head_without_replay(
    tmp_path: Path, window: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    crash_at(tmp_path, window)
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect(SESSION).checkpoint.seq == int(window == "committed")
    finally:
        reader.close()
    # Killing the owner releases its connection; a fresh writer continues.
    api = store(tmp_path)
    try:
        report = api.inspect(SESSION)
        expected_seq = int(window == "committed")
        assert report.checkpoint.seq == expected_seq
        effect = tmp_path / "work" / "effect.txt"
        before = effect.read_bytes() if effect.exists() else None
        assert before == (
            b"one effect\n"
            if window
            in ("checkpoint_insert", "attempt_update", "before_commit", "committed")
            else None
        )
        if window == "before_intent":
            assert report.status == "clean" and report.attempts == ()
        elif window == "committed":
            assert report.status == "clean"
            assert report.receipt.turn_id == report.attempts[0].intent.turn_id
            assert report.attempts[0].state == "completed"
        else:
            assert report.unresolved is not None
            assert report.status == (
                "executing" if window == "intent" else "effects_unknown"
            )
            assert report.checkpoint.messages == ()
            with pytest.raises(SessionStoreError, match="live_turn_required"):
                api.fence_tool(
                    report.revision,
                    fence(
                        bind, ordinal=1 if window == "intent" else 2, call_id="new-call"
                    ),
                )
            report = api.inspect(SESSION)
            ready = api.reconcile(
                report.revision,
                HostResolution(
                    session_id=SESSION,
                    turn_id=report.unresolved.intent.turn_id
                    if report.unresolved
                    else None,
                    actor="test host",
                    resolved_at_utc=T4,
                    binding=bind,
                ),
            )
            assert ready.head_seq == 0
            abandoned = api.inspect(SESSION)
            assert abandoned.attempts[0].state == "abandoned"
            assert abandoned.checkpoint.messages == ()
        for _ in range(2):
            assert api.inspect(SESSION).checkpoint.seq == expected_seq
        assert (effect.read_bytes() if effect.exists() else None) == before
    finally:
        api.close()
