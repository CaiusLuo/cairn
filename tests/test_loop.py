import asyncio
import json
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.budget import BudgetReason, RunBudget, RunBudgetExceeded
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall, ToolFailure, ToolResult
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.llm.litellm_client import LiteLLMClient
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.base import ToolExecutionContext
from cairn.tools.registry import ToolRegistry
from tests.loop_support import (
    TEST_BUDGET,
    FailingLLM,
    FailingSink,
    FailingTool,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    tool_response,
)


class BlockingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingTool:
    name = "block"
    description = "Block until cancelled."

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.calls.append(arguments)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def blocking_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="block",
                arguments={},
            ),
            ToolCall(
                id="call-2",
                name="block",
                arguments={},
            ),
        ]
    )


def assert_serialized_tool_pairs(messages: list[Message]) -> None:
    client = LiteLLMClient(model="test")
    pending: list[str] = []
    for message in messages:
        serialized = client._to_llm_message(message)
        if serialized["role"] == "tool":
            assert pending
            assert serialized["tool_call_id"] == pending.pop(0)
        else:
            assert not pending
            pending = [call["id"] for call in serialized.get("tool_calls", [])]
    assert not pending


def test_run_budget_rejects_invalid_step_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RunBudget(max_steps=0)


@pytest.mark.parametrize("with_tracer", [False, True])
def test_run_turn_cancellation_during_tool_execution_preserves_facts(
    with_tracer: bool,
) -> None:
    async def scenario() -> None:
        sink = RecordingSink()
        events: list[Event] = []

        tool = BlockingTool()
        llm = SequenceLLM([blocking_response()])
        agent = make_agent(llm, tool, events)
        if with_tracer:
            agent.tracer = Tracer(sink)

        task = asyncio.create_task(run_turn(agent, "run tools", budget=TEST_BUDGET))

        await asyncio.wait_for(tool.started.wait(), timeout=1)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
        ]

        cancelled = json.loads(agent.state.messages[2].content or "")
        aborted = json.loads(agent.state.messages[3].content or "")

        assert agent.state.messages[2].tool_call_id == "call-1"
        assert cancelled["type"] == "ToolCancelled"

        assert agent.state.messages[3].tool_call_id == "call-2"
        assert aborted["type"] == "TurnAborted"

        assert tool.calls == [{}]
        assert not any(event.type == "agent_finish" for event in events)
        trace_finish = [event for event in events if event.type == "trace_finish"]
        if with_tracer:
            tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
            turn_spans = [span for span in sink.spans if span.name == "agent.turn"]
            assert len(tool_spans) == len(turn_spans) == 1
            assert tool_spans[0].status == SpanStatus.ERROR
            assert tool_spans[0].attributes["cancelled"] is True
            assert turn_spans[0].status == SpanStatus.ERROR
            assert all(span.end_time is not None for span in sink.spans)
            assert len(trace_finish) == 1
            assert trace_finish[0].data["status"] == "error"
        else:
            assert trace_finish == []

        preserved = agent.state.messages.copy()
        next_llm = SequenceLLM([LLMResponse(content="continued")])
        agent.llm = next_llm
        assert await run_turn(agent, "Continue", budget=TEST_BUDGET) == "continued"
        assert next_llm.calls[0][0][1:] == [
            *preserved,
            Message(role="user", content="Continue"),
        ]
        assert_serialized_tool_pairs(next_llm.calls[0][0])
        assert tool.calls == [{}]

    asyncio.run(scenario())


@pytest.mark.parametrize("with_tracer", [False, True])
def test_run_turn_cancellation_during_llm_wait_rolls_back_and_marks_trace_error(
    with_tracer: bool,
) -> None:
    async def scenario() -> None:
        sink = RecordingSink()
        events: list[Event] = []

        llm = BlockingLLM()
        agent = Agent(
            llm=llm,
            tools=ToolRegistry(),
            event_handler=lambda event: events.append(event),
            tracer=Tracer(sink) if with_tracer else None,
        )

        agent.state.add_user_message("previous")
        agent.state.add_assistant_message("previous answer")
        previous = agent.state.messages.copy()

        task = asyncio.create_task(run_turn(agent, "current", budget=TEST_BUDGET))

        await asyncio.wait_for(llm.started.wait(), timeout=1)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert agent.state.messages == previous

        assert not any(event.type == "agent_finish" for event in events)
        trace_finish = [event for event in events if event.type == "trace_finish"]
        if with_tracer:
            assert [span.name for span in sink.spans] == ["llm.generate", "agent.turn"]
            assert all(span.status == SpanStatus.ERROR for span in sink.spans)
            assert all(span.end_time is not None for span in sink.spans)
            assert sink.spans[0].attributes["cancelled"] is True
            assert len(trace_finish) == 1
            assert trace_finish[0].data["status"] == "error"
        else:
            assert trace_finish == []

    asyncio.run(scenario())


def test_run_turn_preserves_cancellation_when_trace_sink_fails() -> None:
    async def scenario() -> None:
        sink = FailingSink()
        events: list[Event] = []
        llm = BlockingLLM()
        agent = Agent(
            llm=llm,
            tools=ToolRegistry(),
            event_handler=lambda event: events.append(event),
            tracer=Tracer(sink),
        )

        task = asyncio.create_task(run_turn(agent, "current", budget=TEST_BUDGET))
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert agent.state.messages == []
        assert sink.calls == 1
        trace_finish = [event for event in events if event.type == "trace_finish"]
        assert len(trace_finish) == 1
        assert trace_finish[0].data["status"] == "error"
        assert trace_finish[0].data["persisted"] is False
        assert trace_finish[0].data["persistence_error"] == (
            "OSError: simulated trace write failure"
        )

    asyncio.run(scenario())


class BlockingFollowupLLM(SequenceLLM):
    def __init__(self) -> None:
        super().__init__([tool_response()])
        self.started = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if not self.calls:
            return await super().generate(messages, tools)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize("with_tracer", [False, True])
def test_run_turn_cancellation_after_tool_preserves_fact_for_next_turn(
    with_tracer: bool,
) -> None:
    async def scenario() -> None:
        sink = RecordingSink()
        events: list[Event] = []
        tool = RecordingTool()
        llm = BlockingFollowupLLM()
        agent = make_agent(llm, tool, events)
        if with_tracer:
            agent.tracer = Tracer(sink)
        agent.state.add_user_message("previous")
        agent.state.add_assistant_message("previous answer")
        previous = agent.state.messages.copy()

        task = asyncio.create_task(run_turn(agent, "run tool", budget=TEST_BUDGET))
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert agent.state.messages[:2] == previous
        assert [message.role for message in agent.state.messages[2:]] == [
            "user",
            "assistant",
            "tool",
        ]
        assert agent.state.messages[-1].tool_call_id == "call-1"
        assert json.loads(agent.state.messages[-1].content or "") == {
            "stdout": "recorded",
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        assert not any(event.type == "agent_finish" for event in events)
        trace_finish = [event for event in events if event.type == "trace_finish"]
        if with_tracer:
            llm_spans = [span for span in sink.spans if span.name == "llm.generate"]
            assert len(llm_spans) == 2
            assert llm_spans[0].status == SpanStatus.OK
            assert llm_spans[1].status == SpanStatus.ERROR
            assert llm_spans[1].attributes["cancelled"] is True
            assert sink.spans[-1].name == "agent.turn"
            assert sink.spans[-1].status == SpanStatus.ERROR
            assert all(span.end_time is not None for span in sink.spans)
            assert len(trace_finish) == 1
            assert trace_finish[0].data["status"] == "error"
        else:
            assert trace_finish == []

        preserved = agent.state.messages.copy()
        next_llm = SequenceLLM([LLMResponse(content="continued")])
        agent.llm = next_llm
        assert await run_turn(agent, "Continue", budget=TEST_BUDGET) == "continued"
        assert next_llm.calls[0][0][1:] == [
            *preserved,
            Message(role="user", content="Continue"),
        ]
        assert_serialized_tool_pairs(next_llm.calls[0][0])
        assert tool.calls == [{"value": 42}]

    asyncio.run(scenario())


def test_run_turn_returns_direct_model_response() -> None:
    events: list[Event] = []
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = make_agent(llm, events=events)

    result = asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    assert result == "done"
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert [event.type for event in events] == ["agent_step", "agent_finish"]
    assert llm.calls[0][0][0].role == "system"


def test_failed_turn_rolls_back_state() -> None:
    agent = Agent(llm=FailingLLM(), tools=ToolRegistry())
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    previous_messages = agent.state.messages.copy()

    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "current", budget=TEST_BUDGET))

    assert agent.state.messages == previous_messages


def test_run_turn_executes_tool_and_returns_follow_up() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = make_agent(llm, tool, events)
    agent.tracer = Tracer(sink)

    result = asyncio.run(run_turn(agent, "use the tool", budget=TEST_BUDGET))

    assert result == "finished"
    assert tool.calls == [{"value": 42}]
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert json.loads(agent.state.messages[2].content or "") == {
        "stdout": "recorded",
        "stderr": "",
        "exit_code": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    assert [event.type for event in events] == [
        "trace_start",
        "agent_step",
        "tool_call",
        "tool_result",
        "agent_step",
        "agent_finish",
        "trace_finish",
    ]
    tool_result_event = next(event for event in events if event.type == "tool_result")
    assert tool_result_event.data == {
        "tool": "record",
        "exit_code": 0,
        "stdout": "recorded",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    tool_span = tool_spans[0]
    assert tool_span.status == SpanStatus.OK
    assert tool_span.attributes["tool"] == "record"
    assert tool_span.attributes["tool_call_id"] == "call-1"
    assert tool_span.attributes["exit_code"] == 0
    assert tool_span.attributes["stdout_length"] == len("recorded")
    assert tool_span.attributes["stderr_length"] == 0
    assert tool_span.attributes["stdout_truncated"] is False
    assert tool_span.attributes["stderr_truncated"] is False
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert events[0].data == {"trace_id": turn_span.context.trace_id}
    assert events[-1].data == {
        "trace_id": turn_span.context.trace_id,
        "status": "ok",
        "persisted": True,
        "persistence_error": None,
    }
    assert tool_span.context.parent_span_id == turn_span.context.span_id


def test_tool_failure_to_content_contract() -> None:
    failure = ToolFailure(error="boom", type="RuntimeError")

    assert json.loads(failure.to_content()) == {
        "error": "boom",
        "type": "RuntimeError",
    }


def test_run_turn_records_permission_denial_without_executing_tool() -> None:
    sink = RecordingSink()
    llm = SequenceLLM([tool_response(), LLMResponse(content="denied handled")])
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    agent.tracer = Tracer(sink)

    def deny(tool_call: ToolCall) -> PermissionResult:
        assert tool_call.name == "record"
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
        )

    agent.permission_handler = deny

    result = asyncio.run(run_turn(agent, "do not run it", budget=TEST_BUDGET))

    assert result == "denied handled"
    assert tool.calls == []
    permission_spans = [span for span in sink.spans if span.name == "permission.check"]
    assert len(permission_spans) == 1
    permission_span = permission_spans[0]
    assert permission_span.status == SpanStatus.OK
    assert permission_span.attributes["allowed"] is False
    assert permission_span.attributes["tool_call_id"] == "call-1"
    assert json.loads(agent.state.messages[2].content or "") == {
        "error": "Permission denied by user.",
        "type": "PermissionDenied",
    }


def test_run_turn_records_tool_errors_and_continues() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="recovered")])
    agent = make_agent(llm, FailingTool(), events)
    agent.tracer = Tracer(sink)

    result = asyncio.run(run_turn(agent, "run it", budget=TEST_BUDGET))

    assert result == "recovered"
    assert json.loads(agent.state.messages[2].content or "") == {
        "error": "tool failed",
        "type": "RuntimeError",
    }
    assert any(event.type == "tool_error" for event in events)
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    assert tool_spans[0].status == SpanStatus.ERROR
    assert tool_spans[0].error == "RuntimeError: tool failed"
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status == SpanStatus.OK


def test_run_turn_denies_unknown_tool_before_execution() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="recovered")])
    agent = make_agent(llm, events=events)
    agent.tracer = Tracer(sink)

    result = asyncio.run(run_turn(agent, "run it", budget=TEST_BUDGET))

    assert result == "recovered"
    assert json.loads(agent.state.messages[2].content or "") == {
        "error": "Permission denied by user.",
        "type": "PermissionDenied",
    }
    assert not any(event.type == "tool_error" for event in events)
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert tool_spans == []
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status == SpanStatus.OK


class ExitCodeTool(RecordingTool):
    def __init__(self, exit_code: int) -> None:
        super().__init__()
        self.exit_code = exit_code

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(
            stdout="out",
            stderr="err" if self.exit_code != 0 else "",
            exit_code=self.exit_code,
        )


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [
        (0, SpanStatus.OK),
        (3, SpanStatus.ERROR),
        (-1, SpanStatus.ERROR),
    ],
)
def test_run_turn_marks_tool_span_by_exit_code(
    exit_code: int,
    expected_status: SpanStatus,
) -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="recovered")])
    agent = make_agent(llm, ExitCodeTool(exit_code), events)
    agent.tracer = Tracer(sink)

    result = asyncio.run(run_turn(agent, "run it", budget=TEST_BUDGET))

    assert result == "recovered"
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    tool_span = tool_spans[0]
    assert tool_span.status == expected_status
    assert tool_span.attributes["exit_code"] == exit_code
    assert tool_span.attributes["stdout_length"] == len("out")
    assert tool_span.attributes["stderr_length"] == (
        0 if exit_code == 0 else len("err")
    )
    if exit_code == 0:
        assert tool_span.error is None
    else:
        assert tool_span.error is not None
        assert str(exit_code) in tool_span.error
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status == SpanStatus.OK


def test_run_turn_emits_and_raises_at_step_limit() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response()])
    agent = make_agent(llm, RecordingTool(), events)
    agent.tracer = Tracer(sink)

    budget = RunBudget(max_steps=1)
    with pytest.raises(RunBudgetExceeded) as exc_info:
        asyncio.run(run_turn(agent, "keep going", budget=budget))

    exc = exc_info.value
    assert exc.reason == BudgetReason.MAX_STEPS
    assert exc.limit == 1
    assert exc.used == 1
    assert events[-2] == Event(
        type="agent_budget_exhausted",
        data={
            "reason": "max_steps",
            "limit": 1,
            "used": 1,
        },
    )
    assert events[-1].type == "trace_finish"
    assert events[-1].data["status"] == "error"
    assert sink.spans[-1].status == SpanStatus.ERROR


def test_run_turn_uses_caller_budget_to_control_execution_capacity() -> None:
    limited_llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    limited_agent = make_agent(limited_llm, RecordingTool())

    with pytest.raises(RunBudgetExceeded):
        asyncio.run(
            run_turn(
                limited_agent,
                "keep going",
                budget=RunBudget(max_steps=1),
            )
        )

    capable_llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    capable_agent = make_agent(capable_llm, RecordingTool())

    assert (
        asyncio.run(
            run_turn(
                capable_agent,
                "keep going",
                budget=RunBudget(max_steps=2),
            )
        )
        == "done"
    )
    assert len(limited_llm.calls) == 1
    assert len(capable_llm.calls) == 2
