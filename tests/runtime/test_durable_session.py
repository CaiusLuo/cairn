import asyncio
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.events import Event, SessionEvent
from cairn.core.models import LLMResponse, ToolCall, ToolResult
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.codec import restore_messages, snapshot_messages
from cairn.infra.sessions.models import MAX_MESSAGE_BYTES, SessionStoreError, ToolFence
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.runtime.agent import SessionAdmissionError
from cairn.runtime.durable_session import DurableSession, SessionDurabilityError
from cairn.runtime.loop import run_turn
from cairn.runtime.session import Session, SessionState
from cairn.runtime.turn_trace import TurnTrace, TurnTraceSummary
from tests.support.durable_sessions import create_session, resolution
from tests.support.runtime import (
    TEST_BUDGET,
    NetworkRequestTool,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    network_tool_response,
    tool_response,
)
from tests.support.sessions import OTHER_TURN, SESSION, binding, store


def test_intent_precedes_events_state_and_provider_then_full_history_commits(
    tmp_path: Path,
) -> None:
    llm = SequenceLLM(
        [tool_response(), LLMResponse(content="一"), LLMResponse(content="二")]
    )
    events: list[Event] = []
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    api, session = create_session(tmp_path, agent)

    def observe_synchronous(event: Event) -> None:
        events.append(event)
        if event.type == "turn_started":
            report = api.inspect(SESSION)
            assert report.status == "executing" and report.unresolved is not None
            assert len(agent.state.messages) == len(report.checkpoint.messages)
        if event.type in ("turn_settled", "turn_committed"):
            assert session.state is SessionState.EXECUTING
            rejected = run_turn(agent, "reentrant", budget=TEST_BUDGET, session=session)
            with pytest.raises(SessionAdmissionError):
                rejected.send(None)

    agent.event_handler = observe_synchronous
    try:
        assert (
            asyncio.run(run_turn(agent, "first", budget=TEST_BUDGET, session=session))
            == "一"
        )
        first = session.receipt
        first_history = api.inspect(SESSION).checkpoint.messages
        assert (
            asyncio.run(run_turn(agent, "second", budget=TEST_BUDGET, session=session))
            == "二"
        )
        report = api.inspect(SESSION)
        assert report.checkpoint.messages == snapshot_messages(agent.state.messages)
        assert report.checkpoint.messages[: len(first_history)] == first_history
        assert first.revision.head_seq == 1 and session.revision.head_seq == 2
        assert len(tool.calls) == 1 and len(llm.calls) == 3
        assert all(isinstance(event, SessionEvent) for event in events)
        assert [
            event.sequence for event in events if isinstance(event, SessionEvent)
        ] == list(range(1, len(events) + 1))
        committed = [e for e in events if e.type == "turn_committed"]
        assert [e.data["checkpoint_seq"] for e in committed] == [1, 2]
        assert committed[-1].data == {
            "checkpoint_seq": 2,
            "revision": report.revision.version,
            "outcome": "completed",
        }
        assert events[-2].type == "turn_settled" and events[-1].type == "turn_committed"
        assert session.last_turn is not None
        assert not session.last_turn.settlement_delivery_failed
        assert not session.last_turn.commit_delivery_failed
        with pytest.raises(SessionAdmissionError):
            agent.emit(Event(type="turn_committed"))
    finally:
        session.close()


def test_reopen_is_inspection_only_fresh_and_requires_explicit_host_confirmation(
    tmp_path: Path,
) -> None:
    agent = make_agent(
        SequenceLLM([tool_response(), LLMResponse(content="done")]), RecordingTool()
    )
    api, session = create_session(tmp_path, agent)
    asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
    saved = api.inspect(SESSION)
    session.close()
    llm = SequenceLLM([LLMResponse(content="new")])
    tool = RecordingTool()
    events: list[Event] = []
    fresh = make_agent(llm, tool, events)
    reopened = DurableSession.reopen(
        store(tmp_path), SESSION, agent=fresh, binding=saved.binding
    )
    try:
        assert reopened.state is SessionState.SUSPENDED
        assert fresh.state.messages == restore_messages(saved.checkpoint)
        assert not llm.calls and not tool.calls and not events
        # Mutating a restored object cannot change the immutable checkpoint.
        fresh.state.messages[-1].content = "locally changed"
        assert saved.checkpoint.messages[-1].content == "done"
        with pytest.raises(SessionAdmissionError):
            asyncio.run(
                run_turn(fresh, "blocked", budget=TEST_BUDGET, session=reopened)
            )
        report = reopened.recovery_report
        reopened.reconcile(report, host_resolution=resolution(report))
        assert fresh.state.messages == restore_messages(saved.checkpoint)
        assert not llm.calls and not tool.calls and not events
        assert (
            asyncio.run(run_turn(fresh, "new", budget=TEST_BUDGET, session=reopened))
            == "new"
        )
        systems = [m.content or "" for m in llm.calls[0][0] if m.role == "system"]
        assert any("historical" in text for text in systems)
        assert all(m.role != "system" for m in fresh.state.messages)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "case",
    [
        "omitted",
        "wrong",
        "same_id",
        "unbound",
        "closed",
        "store_closed",
        "suspended",
        "history",
        "runtime",
    ],
)
def test_invalid_owner_and_admission_have_zero_effects(
    tmp_path: Path, case: str
) -> None:
    llm = SequenceLLM([network_tool_response(), LLMResponse(content="done")])
    tool = NetworkRequestTool()
    events: list[Event] = []
    agent = make_agent(llm, tool, events)
    prompts: list[object] = []

    def approve(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_ONCE

    agent.permission_handler = SessionPermissionHandler(approve)
    sink = RecordingSink()
    agent.tracer = Tracer(sink)
    api, session = create_session(tmp_path, agent)
    supplied: Session | None = session
    executing_agent = agent
    if case == "omitted":
        supplied = None
    elif case in ("wrong", "same_id"):
        supplied = Session(make_agent(SequenceLLM([])))
        if case == "same_id":
            supplied._session_id = session.session_id
    elif case == "unbound":
        executing_agent = make_agent(SequenceLLM([]))
    elif case == "closed":
        session.close()
    elif case == "store_closed":
        api.close()
    elif case == "suspended":
        session.close()
        api = store(tmp_path)
        agent = make_agent(llm, tool, events)
        executing_agent = agent
        session = DurableSession.reopen(
            api, SESSION, agent=agent, binding=binding(tmp_path)
        )
        supplied = session
    elif case == "history":
        agent.state.add_user_message("not committed")
    elif case == "runtime":
        agent.llm = SequenceLLM([])
    before = agent.state.model_copy(deep=True)
    try:
        with pytest.raises(SessionAdmissionError):
            asyncio.run(
                run_turn(
                    executing_agent, "blocked", budget=TEST_BUDGET, session=supplied
                )
            )
        assert agent.state == before and not events and not prompts
        assert not llm.calls and not tool.calls and not sink.spans
        if case not in ("closed", "store_closed"):
            report = api.inspect(SESSION)
            assert not report.attempts and report.revision.head_seq == 0
        with pytest.raises(SessionAdmissionError):
            Session(agent)
    finally:
        session.close()


@pytest.mark.parametrize(
    "case", ["populated", "grants", "replaced_root", "inspect_store"]
)
def test_failed_attachment_stays_bound_closed(tmp_path: Path, case: str) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    if case == "inspect_store":
        api.create(bind)
        api.close()
        api = store(tmp_path, mode="inspect")
    agent = make_agent(SequenceLLM([]))
    if case == "populated":
        agent.state.add_user_message("retain")
    elif case == "grants":
        handler = SessionPermissionHandler()
        handler.grants.add(PermissionCapability.NETWORK)
        agent.permission_handler = handler
    elif case == "replaced_root":
        work = tmp_path / "work"
        work.rename(tmp_path / "old")
        work.mkdir()
    before = agent.state.model_copy(deep=True)
    try:
        with pytest.raises(SessionAdmissionError):
            DurableSession.create(api, agent=agent, binding=bind)
        assert agent.session_owner is not None and agent.state == before
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "blocked", budget=TEST_BUDGET))
    finally:
        api.close()


@pytest.mark.parametrize("user", ["secret-value", "a" * MAX_MESSAGE_BYTES])
def test_invalid_input_rejected_before_intent(tmp_path: Path, user: str) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path, sensitive_values=("secret-value",))
    llm = SequenceLLM([])
    events: list[Event] = []
    agent = make_agent(llm, events=events)
    session = DurableSession.create(api, agent=agent, binding=bind)
    try:
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, user, budget=TEST_BUDGET, session=session))
        report = api.inspect(SESSION)
        assert report.revision.version == 0 and not report.attempts
        assert not llm.calls and not events and not agent.state.messages
    finally:
        session.close()


@pytest.mark.parametrize("case", ["missing", "malformed", "denied", "approved"])
def test_only_authorized_valid_calls_get_fences_before_execution(
    tmp_path: Path, case: str
) -> None:
    class ObservedTool(NetworkRequestTool):
        def validate(self, arguments: dict[str, Any]) -> None:
            if case == "malformed":
                raise InvalidArguments("bad arguments")

        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            report = api.inspect(SESSION)
            assert report.unresolved is not None
            assert len(report.unresolved.diagnostics.fences) == 1
            assert context is not None
            return await super().execute(arguments, context=context)

    tool = ObservedTool()
    llm = SequenceLLM([network_tool_response(), LLMResponse(content="done")])
    agent = make_agent(llm, tool if case != "missing" else None)
    prompts: list[object] = []

    def approve(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return (
            PermissionChoice.ALLOW_ONCE if case == "approved" else PermissionChoice.DENY
        )

    agent.permission_handler = SessionPermissionHandler(approve)
    api, session = create_session(tmp_path, agent)
    try:
        assert (
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
            == "done"
        )
        report = api.inspect(SESSION)
        assert len(report.attempts[0].diagnostics.fences) == (
            1 if case == "approved" else 0
        )
        assert len(tool.calls) == (1 if case == "approved" else 0)
        assert len(prompts) == (0 if case in ("missing", "malformed") else 1)
        assert snapshot_messages(agent.state.messages) == report.checkpoint.messages
    finally:
        session.close()


def test_multiple_calls_use_one_assistant_index_and_monotone_turn_ordinals(
    tmp_path: Path,
) -> None:
    calls = [ToolCall(id=f"call-{i}", name="record", arguments={}) for i in range(3)]
    llm = SequenceLLM(
        [
            LLMResponse(tool_calls=calls[:2]),
            LLMResponse(tool_calls=calls[2:]),
            LLMResponse(content="done"),
        ]
    )
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    api, session = create_session(tmp_path, agent)
    try:
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        fences = api.inspect(SESSION).attempts[0].diagnostics.fences
        assert [f.ordinal for f in fences] == [1, 2, 3]
        assert [f.assistant_message_index for f in fences] == [1, 1, 4]
        assert len(tool.calls) == 3
    finally:
        session.close()


@pytest.mark.parametrize("fault", ["io", "sensitive"])
def test_fence_failure_stops_tool_outside_error_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    response = tool_response()
    if fault == "sensitive":
        response.tool_calls[0].arguments = {"password": "do-not-persist"}
    tool = RecordingTool()
    llm = SequenceLLM([response, LLMResponse(content="must not retry")])
    events: list[Event] = []
    agent = make_agent(llm, tool, events)
    api, session = create_session(tmp_path, agent)
    if fault == "io":

        def fail_fence(expected: object, fence: ToolFence) -> object:
            raise SessionStoreError("storage_failed")

        monkeypatch.setattr(api, "fence_tool", fail_fence)
    try:
        with pytest.raises(SessionDurabilityError):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        report = session.recovery_report
        assert report.revision.head_seq == 0 and report.unresolved is not None
        assert not report.unresolved.diagnostics.fences
        assert not tool.calls and len(llm.calls) == 1 and not agent.state.messages
        assert not any(
            e.type in ("tool_error", "tool_result", "turn_committed") for e in events
        )
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "blocked", budget=TEST_BUDGET, session=session))
        session.reconcile(report, host_resolution=resolution(report))
        assert session.state is SessionState.READY
    finally:
        session.close()


def test_mutation_then_exception_and_budget_abort_commit_closed_facts(
    tmp_path: Path,
) -> None:
    class MutatingTool(RecordingTool):
        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            (tmp_path / "work" / "effect").write_text("observed")
            raise RuntimeError("mutation failed afterwards")

    llm = SequenceLLM([tool_response()])
    agent = make_agent(llm, MutatingTool())
    api, session = create_session(tmp_path, agent)
    try:
        with pytest.raises(RunBudgetExceeded):
            asyncio.run(
                run_turn(agent, "hello", budget=RunBudget(max_steps=1), session=session)
            )
        report = api.inspect(SESSION)
        assert report.checkpoint.outcome == "budget_exhausted"
        assert report.attempts[0].state == "interrupted"
        assert (tmp_path / "work" / "effect").read_text() == "observed"
        assert (
            json.loads(agent.state.messages[-1].content or "")["type"] == "RuntimeError"
        )
        assert report.checkpoint.messages == snapshot_messages(agent.state.messages)
    finally:
        session.close()


def test_trace_summary_is_immutable_nullable_and_only_available_after_close() -> None:
    agent = make_agent(SequenceLLM([]))
    trace = TurnTrace(agent, TEST_BUDGET)
    assert (trace.summary,) == (None,)
    trace.close(None)
    assert trace.summary == TurnTraceSummary()
    field = "persisted"
    with pytest.raises(FrozenInstanceError):
        setattr(trace.summary, field, True)


def test_empty_response_still_commits_a_complete_turn(tmp_path: Path) -> None:
    agent = make_agent(SequenceLLM([LLMResponse()]))
    api, session = create_session(tmp_path, agent)
    try:
        assert (
            asyncio.run(run_turn(agent, "", budget=TEST_BUDGET, session=session)) == ""
        )
        report = api.inspect(SESSION)
        assert report.checkpoint.outcome == "completed"
        assert [m.content for m in report.checkpoint.messages] == ["", None]
    finally:
        session.close()


def test_reopened_permissions_cannot_reuse_an_old_network_grant(tmp_path: Path) -> None:
    llm = SequenceLLM([network_tool_response(), LLMResponse(content="done")])
    original_tool = NetworkRequestTool()
    agent = make_agent(llm, original_tool)
    handler = SessionPermissionHandler(lambda request: PermissionChoice.ALLOW_SESSION)
    agent.permission_handler = handler
    api, session = create_session(tmp_path, agent)
    asyncio.run(run_turn(agent, "old", budget=TEST_BUDGET, session=session))
    report = api.inspect(SESSION)
    assert handler.grants == {PermissionCapability.LOCAL_SHELL}
    session.close()
    reused = make_agent(SequenceLLM([]))
    reused.permission_handler = handler
    api = store(tmp_path)
    try:
        with pytest.raises(SessionAdmissionError):
            DurableSession.reopen(api, SESSION, agent=reused, binding=report.binding)
    finally:
        api.close()
    fresh_tool = NetworkRequestTool()
    fresh = make_agent(
        SequenceLLM([network_tool_response(), LLMResponse(content="denied")]),
        fresh_tool,
    )
    fresh.permission_handler = SessionPermissionHandler()
    api = store(tmp_path)
    reopened = DurableSession.reopen(api, SESSION, agent=fresh, binding=report.binding)
    try:
        inspected = reopened.recovery_report
        reopened.reconcile(inspected, host_resolution=resolution(inspected))
        assert (
            asyncio.run(run_turn(fresh, "new", budget=TEST_BUDGET, session=reopened))
            == "denied"
        )
        assert not fresh_tool.calls and len(original_tool.calls) == 1
        current = api.inspect(SESSION)
        assert (
            current.checkpoint.messages[: len(report.checkpoint.messages)]
            == report.checkpoint.messages
        )
        assert not current.attempts[-1].diagnostics.fences
    finally:
        reopened.close()


def test_begin_failure_is_sanitized_no_events_or_provider_and_never_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = SequenceLLM([])
    events: list[Event] = []
    agent = make_agent(llm, events=events)
    api, session = create_session(tmp_path, agent)

    def fail_begin(*args: object) -> object:
        raise OSError("private storage diagnostic")

    monkeypatch.setattr(api, "begin_turn", fail_begin)
    try:
        with pytest.raises(SessionDurabilityError) as error:
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        assert "private" not in str(error.value) and not events and not llm.calls
        assert not agent.state.messages and session.active_turn is None
        assert session.state is SessionState.RECOVERY_REQUIRED
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "bare", budget=TEST_BUDGET))
        assert not api.inspect(SESSION).attempts
    finally:
        session.close()


def test_cancelled_begin_keeps_its_own_error_and_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation during admission is never downgraded to a durability error.

    The turn was reserved but no work was dispatched, so the guard is released
    and the session still requires host recovery -- while the caller keeps the
    exact cancellation it raised, with no cause chained onto it.
    """
    llm = SequenceLLM([])
    events: list[Event] = []
    agent = make_agent(llm, events=events)
    api, session = create_session(tmp_path, agent)

    def cancel_begin(*args: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "begin_turn", cancel_begin)
    try:
        with pytest.raises(asyncio.CancelledError) as error:
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        assert error.value.__cause__ is None
        assert not events and not llm.calls and not agent.state.messages
        assert session.active_turn is None
        assert session.state is SessionState.RECOVERY_REQUIRED
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "bare", budget=TEST_BUDGET))
    finally:
        session.close()


@pytest.mark.parametrize("mismatch", ["session_id", "workspace"])
def test_reopen_identity_mismatch_never_restores_or_dispatches(
    tmp_path: Path, mismatch: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    llm = SequenceLLM([])
    agent = make_agent(llm)
    if mismatch == "workspace":
        other = tmp_path / "other"
        other.mkdir()
        bind = binding(other)
    try:
        before = api.inspect(SESSION)
        with pytest.raises(SessionAdmissionError):
            DurableSession.reopen(
                api,
                OTHER_TURN if mismatch == "session_id" else SESSION,
                agent=agent,
                binding=bind,
            )
        assert api.inspect(SESSION) == before
        assert not agent.state.messages and not llm.calls
        assert isinstance(agent.session_owner, DurableSession)
        assert agent.session_owner.state is SessionState.CLOSED
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "bare", budget=TEST_BUDGET))
    finally:
        api.close()


def test_stale_report_cannot_reconcile_a_newer_committed_turn(tmp_path: Path) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = make_agent(llm)
    api, session = create_session(tmp_path, agent)
    try:
        stale = session.recovery_report
        asyncio.run(run_turn(agent, "new", budget=TEST_BUDGET, session=session))
        current = session.recovery_report
        with pytest.raises(SessionAdmissionError):
            session.reconcile(stale, host_resolution=resolution(stale))
        assert api.inspect(SESSION) == current and len(llm.calls) == 1
        session.reconcile(current, host_resolution=resolution(current))
        assert len(llm.calls) == 1 and session.receipt == current.receipt
    finally:
        session.close()
