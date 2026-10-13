import os
import sqlite3
from pathlib import Path

import pytest

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.models import Message, ToolCall
from cairn.core.state import AgentState
from cairn.infra.sessions.codec import canonical, encode_record, restore_messages
from cairn.infra.sessions.models import (
    MAX_RECORD_BYTES,
    MAX_WAL_BYTES,
    AttemptDiagnostics,
    HostResolution,
    SessionStoreError,
    ToolFence,
)
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    binding,
    fence,
    intent,
    settled,
    store,
)


def test_full_agent_history_survives_actual_context_trimming(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        revision = api.create(bind).revision
        old = intent(bind, revision, user="x" * 5000)
        active = api.begin_turn(revision, old)
        state = AgentState(
            messages=[
                Message(role="user", content="x" * 5000),
                Message(role="assistant", content="old response"),
            ]
        )
        receipt = api.commit_turn(active, settled(old, history=state.messages))
        report = api.inspect(SESSION)
        new = intent(
            report.binding, receipt.revision, start=2, turn_id=OTHER_TURN, time=T4
        )
        active = api.begin_turn(receipt.revision, new)
        state.add_user_message("hello")
        request = ContextBuilder(ContextBudget(1000, 100)).build(
            system_messages=[], history=state.messages, current_turn_start=2, tools=[]
        )
        assert request.omitted_messages == 2
        assert len(state.messages) == 3
        state.add_assistant_message("new response")
        receipt = api.commit_turn(active, settled(new, history=state.messages))
        assert restore_messages(api.inspect(SESSION).checkpoint) == state.messages
        assert receipt.revision.head_seq == 2
        assert report.checkpoint.messages[0].content == "x" * 5000
    finally:
        api.close()


def test_exact_attempt_diagnostic_record_limit() -> None:
    # Byte size, not character count; all individual fence fields remain bounded.
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        bind = binding(Path(directory))
        fences = [
            fence(bind, ordinal=i + 1, call_id=str(i), tool_name="r")
            for i in range(256)
        ]
        record = AttemptDiagnostics(binding=bind, message_start=0, fences=tuple(fences))
        remaining = MAX_RECORD_BYTES - len(canonical(record.model_dump(mode="json")))
        assert remaining > 0
        for index in range(len(fences)):
            count = min(remaining, 200)
            fences[index] = fences[index].model_copy(
                update={"tool_name": "r" + "x" * count}
            )
            remaining -= count
            if not remaining:
                break
        assert remaining == 0
        record = record.model_copy(update={"fences": tuple(fences)})
        assert len(encode_record(record)) == MAX_RECORD_BYTES
        fences[-1] = fences[-1].model_copy(update={"tool_name": "rr"})
        with pytest.raises(SessionStoreError, match="record_limit"):
            encode_record(record.model_copy(update={"fences": tuple(fences)}))


def test_256_fenced_invocations_and_terminal_commit(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        for ordinal in range(1, 257):
            active = api.fence_tool(
                active, fence(bind, ordinal=ordinal, call_id=str(ordinal))
            )
        calls = [
            ToolCall(id=str(i), name="read_file", arguments={}) for i in range(1, 257)
        ]
        history = [
            Message(role="user", content="hello"),
            Message(role="assistant", tool_calls=calls),
            *[Message(role="tool", tool_call_id=c.id, content="") for c in calls],
            Message(role="assistant", content="done"),
        ]
        receipt = api.commit_turn(active, settled(turn, history=history))
        assert (receipt.revision.version, receipt.revision.head_seq) == (258, 1)
        report = api.inspect(SESSION)
        assert len(report.attempts[0].diagnostics.fences) == 256
        invalid = fence(bind).model_copy(update={"ordinal": 257})
        with pytest.raises(SessionStoreError):
            api.fence_tool(receipt.revision, invalid)
        assert api.inspect(SESSION).receipt == receipt
    finally:
        api.close()


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_wal_admission_at_threshold_blocks_live_fences(
    tmp_path: Path, extra_bytes: int
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        wal = Path(str(api.path) + "-wal")
        assert wal.stat().st_size > 0
        # Preserve committed frames and pad only the unused physical tail. This
        # tests the real file-byte admission boundary, not a fake SQL receipt.
        os.truncate(wal, MAX_WAL_BYTES + extra_bytes)
        with pytest.raises(SessionStoreError, match="wal_admission_limit"):
            api.fence_tool(active, fence(bind))
        report = api.inspect(SESSION)
        assert report.revision == active
        assert (
            report.unresolved is not None and report.unresolved.diagnostics.fences == ()
        )
        # Idle host reconciliation may reclaim WAL. It abandons, never permits
        # the old invocation or creates an invented tool message.
        ready = api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=turn.turn_id,
                actor="host",
                resolved_at_utc=T4,
                binding=bind,
            ),
        )
        assert api.inspect(SESSION).checkpoint.messages == ()
        assert ready.head_seq == 0 and wal.stat().st_size == 0
    finally:
        api.close()


def test_combined_fence_record_limit_is_checked_before_mutation_transaction(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        prior: tuple[ToolFence, ...] = ()
        for ordinal in range(1, 257):
            candidate = fence(
                bind,
                ordinal=ordinal,
                call_id=f"{ordinal}-" + "c" * 200,
                tool_name="r" * 256,
            )
            report = api.inspect(SESSION)
            assert report.unresolved is not None
            assert api._connection is not None
            statements: list[str] = []
            api._connection.set_trace_callback(statements.append)
            try:
                new = api.fence_tool(active, candidate)
            except SessionStoreError as error:
                api._connection.set_trace_callback(None)
                assert error.code == "record_limit"
                assert "BEGIN IMMEDIATE" not in statements
                report = api.inspect(SESSION)
                assert report.revision == active
                assert (
                    report.unresolved is not None
                    and report.unresolved.diagnostics.fences == prior
                )
                # Every admitted fence must leave capacity for a bounded host
                # resolution, rather than trapping evidence at a full record.
                api.reconcile(
                    report.revision,
                    HostResolution(
                        session_id=SESSION,
                        turn_id=turn.turn_id,
                        actor="🪨" * 256,
                        note="🪨" * 1024,
                        resolved_at_utc=T4,
                        binding=bind,
                    ),
                )
                abandoned = api.inspect(SESSION)
                assert abandoned.attempts[0].state == "abandoned"
                assert abandoned.attempts[0].diagnostics.fences == prior
                break
            else:
                api._connection.set_trace_callback(None)
                active = new
            prior = (*prior, candidate)
        else:
            pytest.fail("diagnostic byte bound was not exercised")
    finally:
        api.close()


def test_wal_maintenance_failure_keeps_successful_receipt(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        assert api._connection is not None

        def authorize(
            action: int,
            name: str | None,
            value: str | None,
            database: str | None,
            source: str | None,
        ) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "wal_checkpoint"
                else sqlite3.SQLITE_OK
            )

        api._connection.set_authorizer(authorize)
        receipt = api.commit_turn(active, settled(turn))
        assert api.maintenance_failed
        api._connection.set_authorizer(None)
        report = api.inspect(SESSION)
        assert report.receipt == receipt and report.status == "clean"
        assert not report.local_fault
    finally:
        api.close()


def test_retained_reader_blocks_wal_reclamation_until_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    reader = sqlite3.connect(api.path, isolation_level=None)
    try:
        initial = api.create(bind).revision
        reader.execute("BEGIN")
        reader.execute("SELECT version FROM sessions").fetchone()
        assert api._connection is not None
        api._connection.execute("PRAGMA busy_timeout=1")
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        receipt = api.commit_turn(active, settled(turn))
        assert api.maintenance_failed
        wal = Path(str(api.path) + "-wal")
        monkeypatch.setattr(
            "cairn.infra.sessions.store.MAX_WAL_BYTES", wal.stat().st_size
        )
        report = api.inspect(SESSION)
        with pytest.raises(SessionStoreError, match="wal_admission_limit"):
            api.begin_turn(
                receipt.revision,
                intent(
                    report.binding,
                    receipt.revision,
                    turn_id=OTHER_TURN,
                    start=4,
                    time=T4,
                ),
            )
        assert api.inspect(SESSION).receipt == receipt
        reader.rollback()
        report = api.inspect(SESSION)
        ready = api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=None,
                actor="host",
                resolved_at_utc=T4,
                binding=report.binding,
            ),
        )
        assert ready.head_seq == 1 and wal.stat().st_size == 0
        assert not api.maintenance_failed
    finally:
        reader.close()
        api.close()


def test_fault_in_one_session_cannot_be_cleared_by_reconciling_another(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    other = binding(tmp_path, session_id=OTHER_TURN)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        api.create(other)
        active = api.begin_turn(initial, intent(bind, initial))
        with pytest.raises(SessionStoreError):
            api.fence_tool(active, fence(bind, ordinal=2))
        report = api.inspect(OTHER_TURN)
        api.reconcile(
            report.revision,
            HostResolution(
                session_id=OTHER_TURN,
                turn_id=None,
                actor="host",
                resolved_at_utc=T4,
                binding=other,
            ),
        )
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.commit_turn(active, settled(intent(bind, initial)))
        report = api.inspect(SESSION)
        assert report.unresolved is not None
        api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=report.unresolved.intent.turn_id,
                actor="host",
                resolved_at_utc=T4,
                binding=bind,
            ),
        )
        assert not api.inspect(SESSION).local_fault
    finally:
        api.close()
