import asyncio
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from litellm.exceptions import RateLimitError

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget, ContextBudgetExceeded, ContextBuilder
from cairn.core.events import Event, SessionEvent
from cairn.core.llm import LLMClient, LLMStreamChunk
from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.core.models import LLMResponse, ToolCall, ToolResult
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime import loop as loop_module
from cairn.runtime.agent import Agent
from cairn.runtime.lifecycle import await_owned
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import ProviderRuntime, ProviderSession
from cairn.runtime.session import (
    Session,
    SessionAdmissionError,
    SessionState,
    TurnOutcome,
)
from tests.support.runtime import (
    TEST_BUDGET,
    FailingSink,
    NetworkRequestTool,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    network_tool_response,
    tool_response,
)
from tests.support.streaming import ScriptedStreamLLM, StreamBarrier


def current_state(session: Session) -> SessionState:
    return session.state


def test_trace_initialization_failure_releases_only_the_admitted_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SequenceLLM([LLMResponse(content="unused")])
    events: list[Event] = []
    agent = Agent(llm, ToolRegistry(), event_handler=lambda event: events.append(event))
    session = Session(agent)

    def fail_initialization(agent: Agent, budget: RunBudget) -> None:
        raise RuntimeError("trace initialization failed")

    monkeypatch.setattr(loop_module, "TurnTrace", fail_initialization)
    with pytest.raises(RuntimeError, match="trace initialization failed"):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
    assert [event.type for event in events] == ["turn_started", "turn_settled"]
    assert llm.calls == [] and agent.state.messages == []
    assert current_state(session) is SessionState.READY
    assert (
        session.last_turn is not None
        and session.last_turn.outcome is TurnOutcome.RUNTIME_ERROR
    )


def test_provider_model_replacement_preserves_the_exact_session_owner() -> None:
    config = ProviderConfig(
        "https://example.test",
        "KEY",
        (ModelConfig("first", ("one",)), ModelConfig("second", ("two",))),
    )
    manager = ModelManager(config)
    builder = ContextBuilder()
    first = SequenceLLM([LLMResponse(content="unused")])
    second = SequenceLLM([LLMResponse(content="selected")])

    def factory(model: str) -> tuple[LLMClient, ContextBuilder]:
        assert model == "two"
        return second, builder

    executor = ModelExecutor(manager, factory)
    agent = Agent(
        first, ToolRegistry(), context_builder=builder, model_executor=executor
    )
    session = Session(agent)
    runtime = ProviderRuntime(
        NamedProvider("host", config), manager, executor, first, builder, factory
    )
    bindings = ProviderSession(runtime, agent=agent)
    bindings.select_model("second")
    assert agent.session_owner is session and agent.llm is second
    assert (
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        == "selected"
    )
    assert first.calls == [] and len(second.calls) == 1
    assert current_state(session) is SessionState.READY


def test_public_emit_cannot_forge_a_session_settlement() -> None:
    events: list[Event] = []
    agent = Agent(
        SequenceLLM([]),
        ToolRegistry(),
        event_handler=lambda event: events.append(event),
    )
    session = Session(agent)
    with pytest.raises(SessionAdmissionError):
        agent.emit(Event(type="turn_settled", data={"outcome": "completed"}))
    assert events == [] and session.active_turn is session.last_turn is None
    assert current_state(session) is SessionState.READY


@pytest.mark.parametrize(
    "case", ["omitted", "none", "wrong", "same_id", "unbound", "closed"]
)
def test_invalid_admission_has_zero_observations_or_effects(case: str) -> None:
    llm = SequenceLLM([network_tool_response(), LLMResponse(content="done")])
    tool = NetworkRequestTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []
    prompts: list[ToolCall] = []
    sink = RecordingSink()

    def permission(tool_call: ToolCall) -> PermissionResult:
        prompts.append(tool_call)
        return PermissionResult(policy_decision=PermissionDecision.ASK, allowed=False)

    agent = Agent(
        llm,
        registry,
        event_handler=lambda event: events.append(event),
        permission_handler=permission,
        tracer=Tracer(sink),
    )
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    before = [message.model_copy(deep=True) for message in agent.state.messages]
    owner = None if case == "unbound" else Session(agent)
    supplied = owner
    if case in ("wrong", "same_id", "unbound"):
        supplied = Session(Agent(SequenceLLM([]), ToolRegistry()))
        if case == "same_id":
            assert owner is not None
            supplied._session_id = owner.session_id
    if case == "closed":
        assert owner is not None
        owner.close()
    if case in ("omitted", "none"):
        supplied = None
    owner_state = owner.state if owner else None
    with pytest.raises(SessionAdmissionError):
        if case == "omitted":
            asyncio.run(run_turn(agent, "rejected", budget=TEST_BUDGET))
        else:
            asyncio.run(
                run_turn(agent, "rejected", budget=TEST_BUDGET, session=supplied)
            )
    assert llm.calls == []
    assert tool.calls == []
    assert prompts == []
    assert events == []
    assert sink.spans == []
    assert agent.state.messages == before
    assert agent.session_owner is owner
    assert agent._stateless_turns == 0
    if owner:
        assert (
            owner.state is owner_state and owner.active_turn is owner.last_turn is None
        )
    if supplied:
        assert supplied.active_turn is supplied.last_turn is None


def test_binding_is_read_only_once_only_and_failed_attachment_stays_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SequenceLLM([LLMResponse(content="unused")])
    events: list[Event] = []
    sink = RecordingSink()
    agent = Agent(
        llm,
        ToolRegistry(),
        event_handler=lambda event: events.append(event),
        tracer=Tracer(sink),
    )
    original = Agent._bind_session

    def fail_after_binding(
        self: Agent, session: object, attribute_event: Callable[[Event], Event]
    ) -> None:
        original(self, session, attribute_event)
        raise RuntimeError("attachment failed")

    monkeypatch.setattr(Agent, "_bind_session", fail_after_binding)
    with pytest.raises(RuntimeError, match="attachment failed"):
        Session(agent)
    owner = agent.session_owner
    assert isinstance(owner, Session) and owner.state is SessionState.CLOSED
    with pytest.raises(AttributeError):
        attribute = "session_owner"
        setattr(agent, attribute, None)
    with pytest.raises(SessionAdmissionError):
        Session(agent)
    for supplied in (None, owner):
        with pytest.raises(SessionAdmissionError):
            asyncio.run(
                run_turn(agent, "no downgrade", budget=TEST_BUDGET, session=supplied)
            )
    assert agent.session_owner is owner
    assert llm.calls == []
    assert events == []
    assert sink.spans == []
    assert agent.state.messages == []


def test_two_turns_have_independent_uuid4_identity_and_ordered_envelopes() -> None:
    events: list[Event] = []
    sink = RecordingSink()
    llm = SequenceLLM([LLMResponse(content="first"), LLMResponse(content="second")])
    agent = Agent(
        llm,
        ToolRegistry(),
        event_handler=lambda event: events.append(event),
        tracer=Tracer(sink),
    )
    session = Session(agent)
    assert session.agent is agent and current_state(session) is SessionState.READY
    assert (
        asyncio.run(run_turn(agent, "one", budget=TEST_BUDGET, session=session))
        == "first"
    )
    first = session.last_turn
    assert first is not None and first.outcome is TurnOutcome.COMPLETED
    assert (
        asyncio.run(run_turn(agent, "two", budget=TEST_BUDGET, session=session))
        == "second"
    )
    second = session.last_turn
    assert second is not None and second.outcome is TurnOutcome.COMPLETED
    assert first is not second and first.turn_id != second.turn_id
    identifiers = {session.session_id, first.turn_id, second.turn_id, uuid4()}
    traces = [
        UUID(event.data["trace_id"]) for event in events if event.type == "trace_start"
    ]
    assert len(identifiers.union(traces)) == 6
    assert all(identifier.version == 4 for identifier in identifiers)
    assert all(isinstance(event, SessionEvent) for event in events)
    envelopes = [event for event in events if isinstance(event, SessionEvent)]
    assert [event.sequence for event in envelopes] == list(range(1, len(events) + 1))
    assert all(event.session_id == session.session_id for event in envelopes)
    for turn in (first, second):
        group = [event for event in envelopes if event.turn_id == turn.turn_id]
        assert group[0].type == "turn_started" and group[-1].type == "turn_settled"
        assert group[-2].type == "trace_finish"
        assert turn.start_sequence == group[0].sequence
        assert turn.end_sequence == group[-1].sequence
    assert current_state(session) is SessionState.READY and session.active_turn is None
    assert [message.content for message in agent.state.messages] == [
        "one",
        "first",
        "two",
        "second",
    ]
    with pytest.raises(FrozenInstanceError):
        attribute = "outcome"
        setattr(first, attribute, TurnOutcome.RUNTIME_ERROR)
    assert not hasattr(session, "events")  # No retained delivery queue.


def test_rebinding_and_closed_owner_never_downgrade_to_stateless() -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = Agent(llm, ToolRegistry())
    session = Session(agent)
    with pytest.raises(SessionAdmissionError):
        Session(agent)
    session.close()
    session.close()
    assert (
        current_state(session) is SessionState.CLOSED and agent.session_owner is session
    )
    with pytest.raises(SessionAdmissionError):
        asyncio.run(run_turn(agent, "bare", budget=TEST_BUDGET))
    assert llm.calls == []
    assert agent.state.messages == []


@pytest.mark.parametrize(
    "phase", ["turn_started", "agent_step", "trace_finish", "turn_settled"]
)
def test_reentrant_admission_is_rejected_while_events_are_delivered(phase: str) -> None:
    llm = SequenceLLM(
        [LLMResponse(content="done"), LLMResponse(content="must not run")]
    )
    events: list[Event] = []
    sink = RecordingSink()
    agent = Agent(llm, ToolRegistry(), tracer=Tracer(sink))
    session = Session(agent)
    rejected = False

    def observe(event: Event) -> None:
        nonlocal rejected
        events.append(event)
        if event.type != phase:
            return
        assert current_state(session) is SessionState.EXECUTING
        before = (
            list(agent.state.messages),
            len(llm.calls),
            len(events),
            len(sink.spans),
            session.active_turn,
        )
        reentrant = run_turn(agent, "reentrant", budget=TEST_BUDGET, session=session)
        try:
            with pytest.raises(SessionAdmissionError):
                reentrant.send(None)
        finally:
            reentrant.close()
        assert before == (
            list(agent.state.messages),
            len(llm.calls),
            len(events),
            len(sink.spans),
            session.active_turn,
        )
        rejected = True

    agent.event_handler = observe
    assert (
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        == "done"
    )
    assert rejected and len(llm.calls) == 1
    assert current_state(session) is SessionState.READY


def test_binding_during_stateless_work_is_rejected_without_changing_that_turn() -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = Agent(llm, ToolRegistry())

    def observe(event: Event) -> None:
        assert type(event) is Event
        with pytest.raises(SessionAdmissionError):
            Session(agent)

    agent.event_handler = observe
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"
    assert agent.session_owner is None and agent._stateless_turns == 0
    Session(agent)  # Attachment becomes valid only after the existing work ends.


def test_overlapping_admission_has_zero_additional_work() -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        llm = ScriptedStreamLLM(
            [
                LLMStreamChunk(text="partial"),
                blocked,
                LLMStreamChunk(finish_reason="stop"),
            ]
        )
        events: list[Event] = []
        sink = RecordingSink()
        tool = RecordingTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        agent = Agent(
            llm,
            registry,
            event_handler=lambda event: events.append(event),
            tracer=Tracer(sink),
        )
        session = Session(agent)
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        await blocked.entered.wait()
        before = (
            list(agent.state.messages),
            len(llm.calls),
            len(events),
            len(sink.spans),
            session.active_turn,
        )
        with pytest.raises(SessionAdmissionError):
            await run_turn(agent, "overlap", budget=TEST_BUDGET, session=session)
        assert before == (
            list(agent.state.messages),
            len(llm.calls),
            len(events),
            len(sink.spans),
            session.active_turn,
        )
        assert tool.calls == [] and current_state(session) is SessionState.EXECUTING
        blocked.release.set()
        assert await task == "partial"
        assert current_state(session) is SessionState.READY

    asyncio.run(scenario())


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize("trace_fault", [False, True])
def test_repeated_stream_cancellation_keeps_ownership_through_cleanup(
    close: bool, trace_fault: bool
) -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        cleanup = StreamBarrier(asyncio.Event(), asyncio.Event())
        llm = ScriptedStreamLLM([LLMStreamChunk(text="secret provisional"), blocked])
        llm.cleanup_barrier = cleanup
        events: list[Event] = []

        def observe(event: Event) -> None:
            events.append(event)
            if trace_fault and event.type == "trace_finish":
                raise RuntimeError("private trace diagnostic")

        agent = Agent(
            llm, ToolRegistry(), event_handler=observe, tracer=Tracer(RecordingSink())
        )
        session = Session(agent)
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        await blocked.entered.wait()
        active = session.active_turn
        task.cancel()
        await cleanup.entered.wait()
        task.cancel()
        if close:
            session.close()
        assert (
            session.active_turn is active
            and current_state(session) is SessionState.EXECUTING
        )
        assert not any(event.type == "turn_settled" for event in events)
        with pytest.raises(SessionAdmissionError):
            await run_turn(agent, "still busy", budget=TEST_BUDGET, session=session)
        cleanup.release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert session.active_turn is None
        assert current_state(session) is (
            SessionState.CLOSED if close else SessionState.READY
        )
        assert llm.closed == 1 and agent.state.messages == []
        assert session.last_turn is not None
        assert session.last_turn.outcome is TurnOutcome.CANCELLED
        assert session.last_turn.trace_finalization_failed is trace_fault
        if trace_fault:
            assert error.value.__notes__ == ["Turn trace finalization failed."]
        assert events[-1].type == "turn_settled"
        assert "secret" not in repr(session.last_turn) and "private" not in repr(
            session.last_turn
        )
        assert not [
            other
            for other in asyncio.all_tasks()
            if other is not asyncio.current_task() and not other.done()
        ]

    asyncio.run(scenario())


def test_close_during_successful_work_defers_closed_state_until_settlement() -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        llm = ScriptedStreamLLM(
            [
                blocked,
                LLMStreamChunk(text="done"),
                LLMStreamChunk(finish_reason="stop"),
            ]
        )
        agent = Agent(llm, ToolRegistry())
        session = Session(agent)
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        await blocked.entered.wait()
        session.close()
        assert (
            session.close_requested and current_state(session) is SessionState.EXECUTING
        )
        blocked.release.set()
        assert await task == "done"
        assert (
            current_state(session) is SessionState.CLOSED
            and agent.session_owner is session
        )
        assert (
            session.last_turn is not None
            and session.last_turn.outcome is TurnOutcome.COMPLETED
        )

    asyncio.run(scenario())


def test_cancelled_tool_records_facts_before_settlement_and_finishes_cleanup() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()

        class OwnedTool(RecordingTool):
            async def execute(
                self,
                arguments: dict[str, Any],
                *,
                context: ToolExecutionContext | None = None,
            ) -> ToolResult:
                self.calls.append(arguments)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:

                    async def cleanup() -> None:
                        closing.set()
                        await release.wait()

                    await await_owned(asyncio.create_task(cleanup()))
                raise AssertionError("Tool must be cancelled")

        tool = OwnedTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        group = tool_response()
        group.tool_calls.append(
            ToolCall(id="later", name="record", arguments={"value": 2})
        )
        agent = Agent(SequenceLLM([group]), registry)
        session = Session(agent)
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        await started.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        assert current_state(session) is SessionState.EXECUTING
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(tool.calls) == 1
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
        ]
        assert (
            json.loads(agent.state.messages[2].content or "")["type"] == "ToolCancelled"
        )
        assert (
            json.loads(agent.state.messages[3].content or "")["type"] == "TurnAborted"
        )
        assert (
            session.last_turn is not None
            and session.last_turn.outcome is TurnOutcome.CANCELLED
        )
        assert current_state(session) is SessionState.READY

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["agent_finish", "trace_finish", "turn_settled"])
@pytest.mark.parametrize("with_tool", [False, True])
def test_callback_failures_keep_existing_fact_boundary(
    phase: str, with_tool: bool
) -> None:
    responses = [LLMResponse(content="done")]
    if with_tool:
        responses.insert(0, tool_response())
    llm = SequenceLLM(responses)
    tool = RecordingTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []

    def observe(event: Event) -> None:
        events.append(event)
        if event.type == phase:
            raise RuntimeError("private observer failure")

    agent = Agent(llm, registry, event_handler=observe, tracer=Tracer(RecordingSink()))
    session = Session(agent)
    if phase == "turn_settled":
        assert (
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
            == "done"
        )
    else:
        with pytest.raises(RuntimeError, match="private observer failure"):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
    assert (
        current_state(session) is SessionState.READY and session.last_turn is not None
    )
    assert session.last_turn.outcome is (
        TurnOutcome.COMPLETED if phase == "turn_settled" else TurnOutcome.RUNTIME_ERROR
    )
    assert session.last_turn.settlement_delivery_failed is (phase == "turn_settled")
    assert session.last_turn.trace_finalization_failed is (phase == "trace_finish")
    if with_tool:
        assert len(tool.calls) == 1
        assert json.loads(agent.state.messages[2].content or "")["exit_code"] == 0
    elif phase != "turn_settled":
        assert agent.state.messages == []
    assert events[-1].type == "turn_settled"
    assert "private" not in repr(session.last_turn)


@pytest.mark.parametrize("failure", ["provider", "step_budget", "context_budget"])
def test_runtime_failures_have_precise_settled_outcomes(failure: str) -> None:
    llm = ScriptedStreamLLM([RuntimeError("provider failure")])
    registry = ToolRegistry()
    registry.register_tool(RecordingTool())
    agent = Agent(llm, registry)
    budget = TEST_BUDGET
    exception: type[Exception] = RuntimeError
    outcome = TurnOutcome.RUNTIME_ERROR
    if failure == "step_budget":
        agent.llm = SequenceLLM([tool_response()])
        budget = RunBudget(max_steps=1)
        exception, outcome = RunBudgetExceeded, TurnOutcome.BUDGET_EXHAUSTED
    elif failure == "context_budget":
        agent.context_builder = ContextBuilder(
            ContextBudget(max_tokens=2, response_tokens=1)
        )
        exception, outcome = ContextBudgetExceeded, TurnOutcome.BUDGET_EXHAUSTED
    session = Session(agent)
    with pytest.raises(exception):
        asyncio.run(run_turn(agent, "hello", budget=budget, session=session))
    assert session.last_turn is not None and session.last_turn.outcome is outcome
    assert current_state(session) is SessionState.READY


def test_mutation_then_error_and_sink_failure_keep_observed_facts(
    tmp_path: Path,
) -> None:
    class MutatingTool(RecordingTool):
        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            (tmp_path / "effect").write_text("once")
            self.calls.append(arguments)
            raise RuntimeError("mutated then failed")

    tool = MutatingTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []
    agent = Agent(
        SequenceLLM([tool_response(), LLMResponse(content="done")]),
        registry,
        tracer=Tracer(FailingSink()),
        event_handler=lambda event: events.append(event),
    )
    session = Session(agent)
    assert (
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        == "done"
    )
    assert (tmp_path / "effect").read_text() == "once" and len(tool.calls) == 1
    assert json.loads(agent.state.messages[2].content or "")["type"] == "RuntimeError"
    assert (
        session.last_turn is not None
        and session.last_turn.outcome is TurnOutcome.COMPLETED
    )
    finish = next(event for event in events if event.type == "trace_finish")
    assert finish.data["persisted"] is False
    assert session.last_turn.trace_finalization_failed is False


def test_streaming_fallback_events_keep_turn_step_and_attempt_identity() -> None:
    first = ScriptedStreamLLM(
        [
            LLMStreamChunk(text="abandoned"),
            RateLimitError(message="rate", model="one", llm_provider="openai"),
        ]
    )
    second = ScriptedStreamLLM(
        [LLMStreamChunk(text="done"), LLMStreamChunk(finish_reason="stop")]
    )
    manager = ModelManager(
        ProviderConfig(
            "https://example.test", "KEY", (ModelConfig("group", ("one", "two")),)
        )
    )
    agent = Agent(first, ToolRegistry())
    agent.model_executor = ModelExecutor(
        manager, lambda model: (second, agent.context_builder)
    )
    events: list[Event] = []
    agent.event_handler = lambda event: events.append(event)
    session = Session(agent)
    assert (
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        == "done"
    )
    deltas = [event for event in events if event.type == "model_text_delta"]
    assert [event.data["attempt"] for event in deltas] == [1, 2]
    assert [event.data["step"] for event in deltas] == [1, 1]
    assert all(
        isinstance(event, SessionEvent)
        and event.session_id == session.session_id
        and session.last_turn is not None
        and event.turn_id == session.last_turn.turn_id
        for event in events
    )
    assert first.closed == second.closed == 1
    assert agent.state.messages[-1].content == "done"
    assert events[-1].type == "turn_settled"
