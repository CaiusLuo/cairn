import asyncio

import pytest

from cairn.core.agent import Agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.registry import ToolRegistry
from tests.loop_support import (
    FailingLLM,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    tool_response,
)


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


@pytest.mark.parametrize("with_handler", [False, True])
def test_run_turn_ends_allowed_permission_span_once(with_handler: bool) -> None:
    sink = RecordingSink()
    llm = SequenceLLM([tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = make_agent(llm, tool)
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
    agent = make_agent(SequenceLLM([tool_response()]), tool)
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


def test_run_turn_marks_root_trace_as_error_at_step_limit() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    llm = SequenceLLM([tool_response()])
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
