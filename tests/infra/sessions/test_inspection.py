"""Read-only inspection observes committed state and refuses mutation."""

from pathlib import Path

import pytest

from cairn.agent.session.models import SessionStoreError
from tests.support.sessions import SESSION, binding, fence, intent, settled, store


def test_inspection_reads_committed_state_and_refuses_writes(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    writer = store(tmp_path)
    initial = writer.create(bind).revision
    turn = intent(bind, initial)
    active = writer.fence_tool(writer.begin_turn(initial, turn), fence(bind))
    reader = store(tmp_path, mode="inspect")
    try:
        for _ in range(2):
            report = reader.inspect(SESSION)
            assert report.revision == active
            assert report.checkpoint.seq == 0
            assert report.attempts[0].diagnostics.fences == (fence(bind),)
            assert report.status == "effects_unknown"
        with pytest.raises(SessionStoreError, match="read_only_store"):
            reader.begin_turn(report.revision, intent(bind, report.revision))
        writer.commit_turn(active, settled(turn))
        committed = reader.inspect(SESSION)
        assert committed.checkpoint.seq == 1
        assert committed.status == "clean"
        assert committed.receipt.turn_id == turn.turn_id
    finally:
        reader.close()
        writer.close()
