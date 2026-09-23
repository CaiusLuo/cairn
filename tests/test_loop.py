import asyncio
import json

import pytest

from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from tests.loop_support import (
    FailingTool,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    tool_response,
)


def test_run_turn_returns_direct_model_response() -> None:
    events: list[Event] = []
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = make_agent(llm, events=events)

    result = asyncio.run(run_turn(agent, "hello"))

    assert result == "done"
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert [event.type for event in events] == ["agent_step", "agent_finish"]
    assert llm.calls[0][0][0].role == "system"


def test_run_turn_executes_tool_and_returns_follow_up() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = make_agent(llm, tool, events)
    agent.tracer = Tracer(sink)

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
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    tool_span = tool_spans[0]
    assert tool_span.status == SpanStatus.OK
    assert tool_span.attributes["tool"] == "record"
    assert tool_span.attributes["tool_call_id"] == "call-1"
    assert tool_span.attributes["exit_code"] == 0
    assert tool_span.attributes["stdout_length"] == len("recorded")
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert tool_span.context.parent_span_id == turn_span.context.span_id


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


def test_run_turn_records_tool_errors_and_continues() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([tool_response(), LLMResponse(content="recovered")])
    agent = make_agent(llm, FailingTool(), events)
    agent.tracer = Tracer(sink)

    result = asyncio.run(run_turn(agent, "run it"))

    assert result == "recovered"
    assert json.loads(agent.state.messages[2].content or "") == {
        "err": "tool failed",
        "type": "RuntimeError",
    }
    assert any(event.type == "tool_error" for event in events)
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    assert tool_spans[0].status == SpanStatus.ERROR
    assert tool_spans[0].error == "RuntimeError: tool failed"
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status == SpanStatus.OK


def test_run_turn_emits_and_raises_at_step_limit() -> None:
    events: list[Event] = []
    llm = SequenceLLM([tool_response()])
    agent = make_agent(llm, RecordingTool(), events)

    with pytest.raises(RuntimeError, match="Agent exceeded maximum steps: 1"):
        asyncio.run(run_turn(agent, "keep going", max_steps=1))

    assert events[-1] == Event(type="agent_step_limit", data={"max_steps": 1})
