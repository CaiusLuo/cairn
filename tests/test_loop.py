import asyncio
import json
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.observability.models import Span, SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.base import Tool
from cairn.tools.registry import ToolRegistry


class SequenceLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[dict[str, Any]] | None]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append((messages, tools))
        return self.responses.pop(0)


class FailingLLM:
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        raise RuntimeError("llm failed")


class RecordingTool:
    name = "record"
    description = "Record arguments and return a deterministic result."

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(stdout="recorded", exit_code=0)


class FailingTool(RecordingTool):
    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("tool failed")


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def emit(self, span: Span) -> None:
        self.spans.append(span)


def _agent(
    llm: SequenceLLM,
    tool: Tool | None = None,
    events: list[Event] | None = None,
) -> Agent:
    registry = ToolRegistry()
    if tool is not None:
        registry.register_tool(tool)

    def handle_event(event: Event) -> None:
        if events is not None:
            events.append(event)

    return Agent(
        llm=llm,
        tools=registry,
        event_handler=None if events is None else handle_event,
    )


def _tool_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="record",
                arguments={"value": 42},
            )
        ]
    )


def test_run_turn_returns_direct_model_response() -> None:
    events: list[Event] = []
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = _agent(llm, events=events)

    result = asyncio.run(run_turn(agent, "hello"))

    assert result == "done"
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert [event.type for event in events] == ["agent_step", "agent_finish"]
    assert llm.calls[0][0][0].role == "system"


def test_run_turn_emits_root_trace() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        tracer=tracer,
    )

    result = asyncio.run(run_turn(agent, "hello"))

    assert result == "done"
    assert len(sink.spans) == 2

    llm_span = sink.spans[0]
    turn_span = sink.spans[1]

    assert llm_span.name == "llm.generate"
    assert llm_span.status == SpanStatus.OK

    assert turn_span.name == "agent.turn"
    assert turn_span.status == SpanStatus.OK

    assert llm_span.context.trace_id == turn_span.context.trace_id
    assert llm_span.context.parent_span_id == turn_span.context.span_id
    assert llm_span.attributes["step"] == 1


def test_run_turn_marks_llm_and_root_traces_as_error() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    agent = Agent(
        llm=FailingLLM(),
        tools=ToolRegistry(),
        tracer=tracer,
    )

    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "hello"))

    assert len(sink.spans) == 2

    llm_span = sink.spans[0]
    turn_span = sink.spans[1]

    assert llm_span.name == "llm.generate"
    assert llm_span.status == SpanStatus.ERROR
    assert llm_span.error == "RuntimeError: llm failed"

    assert turn_span.name == "agent.turn"
    assert turn_span.status == SpanStatus.ERROR
    assert turn_span.error == "RuntimeError: llm failed"

    assert llm_span.context.trace_id == turn_span.context.trace_id
    assert llm_span.context.parent_span_id == turn_span.context.span_id


def test_run_turn_executes_tool_and_returns_follow_up() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = _agent(llm, tool, events)

    result = asyncio.run(run_turn(agent, "use the tool"))

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
    }
    assert [event.type for event in events] == [
        "agent_step",
        "tool_call",
        "tool_result",
        "agent_step",
        "agent_finish",
    ]


def test_run_turn_records_permission_denial_without_executing_tool() -> None:
    sink = RecordingSink()
    llm = SequenceLLM([_tool_response(), LLMResponse(content="denied handled")])
    tool = RecordingTool()
    agent = _agent(llm, tool)
    agent.tracer = Tracer(sink)

    def deny(tool_call: ToolCall) -> PermissionResult:
        assert tool_call.name == "record"
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
        )

    agent.permission_handler = deny

    result = asyncio.run(run_turn(agent, "do not run it"))

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


@pytest.mark.parametrize("with_handler", [False, True])
def test_run_turn_ends_allowed_permission_span_once(with_handler: bool) -> None:
    sink = RecordingSink()
    llm = SequenceLLM([_tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = _agent(llm, tool)
    agent.tracer = Tracer(sink)

    if with_handler:

        def allow(tool_call: ToolCall) -> PermissionResult:
            assert tool_call.name == "record"
            return PermissionResult(
                policy_decision=PermissionDecision.ALLOW,
                allowed=True,
            )

        agent.permission_handler = allow

    assert asyncio.run(run_turn(agent, "use the tool")) == "finished"
    assert tool.calls == [{"value": 42}]

    permission_spans = [span for span in sink.spans if span.name == "permission.check"]
    assert len(permission_spans) == 1
    permission_span = permission_spans[0]
    assert permission_span.status == SpanStatus.OK
    assert permission_span.attributes["allowed"] is True
    assert permission_span.attributes["tool_call_id"] == "call-1"
    assert permission_span.attributes["handler_configured"] is with_handler

    if with_handler:
        assert permission_span.attributes["policy_decision"] == "allow"
        assert "source" not in permission_span.attributes
    else:
        assert permission_span.attributes["source"] == "no_handler"


def test_run_turn_ends_permission_span_once_when_handler_raises() -> None:
    sink = RecordingSink()
    tool = RecordingTool()
    agent = _agent(SequenceLLM([_tool_response()]), tool)
    agent.tracer = Tracer(sink)

    def fail(tool_call: ToolCall) -> PermissionResult:
        raise RuntimeError("permission failed")

    agent.permission_handler = fail

    with pytest.raises(RuntimeError, match="permission failed"):
        asyncio.run(run_turn(agent, "use the tool"))

    assert tool.calls == []
    permission_spans = [span for span in sink.spans if span.name == "permission.check"]
    assert len(permission_spans) == 1
    assert permission_spans[0].status == SpanStatus.ERROR
    assert permission_spans[0].error == "RuntimeError: permission failed"


def test_run_turn_records_tool_errors_and_continues() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response(), LLMResponse(content="recovered")])
    agent = _agent(llm, FailingTool(), events)

    result = asyncio.run(run_turn(agent, "run it"))

    assert result == "recovered"
    assert json.loads(agent.state.messages[2].content or "") == {
        "err": "tool failed",
        "type": "RuntimeError",
    }
    assert any(event.type == "tool_error" for event in events)


def test_run_turn_emits_and_raises_at_step_limit() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response()])
    agent = _agent(llm, RecordingTool(), events)

    with pytest.raises(RuntimeError, match="Agent exceeded maximum steps: 1"):
        asyncio.run(run_turn(agent, "keep going", max_steps=1))

    assert events[-1] == Event(type="agent_step_limit", data={"max_steps": 1})


def test_run_turn_marks_root_trace_as_error_at_step_limit() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    llm = SequenceLLM([_tool_response()])
    registry = ToolRegistry()
    registry.register_tool(RecordingTool())
    agent = Agent(
        llm=llm,
        tools=registry,
        tracer=tracer,
    )

    with pytest.raises(RuntimeError, match="Agent exceeded maximum steps: 1"):
        asyncio.run(run_turn(agent, "keep going", max_steps=1))

    span = sink.spans[-1]

    assert span.name == "agent.turn"
    assert span.status == SpanStatus.ERROR
    assert span.error is not None
