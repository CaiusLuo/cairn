import asyncio

import pytest

from cairn.core.agent import Agent
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.registry import ToolRegistry
from tests.support.runtime import (
    TEST_BUDGET,
    FailingLLM,
    FailingSink,
    NetworkRequestTool,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    network_tool_response,
    tool_response,
)


def test_run_turn_emits_root_trace() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    llm = SequenceLLM(
        [
            LLMResponse(
                content="done",
                usage=LLMUsage(input_tokens=12, output_tokens=3),
            )
        ]
    )
    events: list[Event] = []
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        tracer=tracer,
        event_handler=lambda event: events.append(event),
    )

    result = asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    assert result == "done"
    assert len(sink.spans) == 2

    llm_span = sink.spans[0]
    turn_span = sink.spans[1]

    assert llm_span.name == "llm.generate"
    assert llm_span.status == SpanStatus.OK
    assert llm_span.attributes["input_tokens"] == 12
    assert llm_span.attributes["output_tokens"] == 3

    assert turn_span.name == "agent.turn"
    assert turn_span.status == SpanStatus.OK
    assert turn_span.attributes["input_tokens"] == 12
    assert turn_span.attributes["output_tokens"] == 3

    assert llm_span.context.trace_id == turn_span.context.trace_id
    assert llm_span.context.parent_span_id == turn_span.context.span_id
    assert llm_span.attributes["step"] == 1
    assert [event for event in events if event.type == "trace_finish"] == [
        Event(
            type="trace_finish",
            data={
                "trace_id": turn_span.context.trace_id,
                "status": "ok",
                "persisted": True,
                "persistence_error": None,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                },
            },
        )
    ]


def test_run_turn_keeps_missing_usage_unknown_in_trace() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
        event_handler=lambda event: events.append(event),
    )

    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"

    llm_span = sink.spans[0]
    turn_span = sink.spans[1]
    assert "input_tokens" not in llm_span.attributes
    assert "output_tokens" not in llm_span.attributes
    assert "input_tokens" not in turn_span.attributes
    assert "output_tokens" not in turn_span.attributes
    assert "usage" not in events[-1].data


def test_run_turn_aggregates_usage_across_llm_calls() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    first_response = tool_response()
    first_response.usage = LLMUsage(input_tokens=10, output_tokens=2)
    agent = make_agent(
        SequenceLLM(
            [
                first_response,
                LLMResponse(
                    content="done",
                    usage=LLMUsage(input_tokens=20, output_tokens=3),
                ),
            ]
        ),
        RecordingTool(),
        events,
    )
    agent.tracer = Tracer(sink)

    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"

    turn_span = sink.spans[-1]
    assert turn_span.name == "agent.turn"
    assert turn_span.attributes["input_tokens"] == 30
    assert turn_span.attributes["output_tokens"] == 5
    assert events[-1].data["usage"] == {
        "input_tokens": 30,
        "output_tokens": 5,
    }


def test_run_turn_marks_usage_unknown_when_later_llm_call_fails() -> None:
    sink = RecordingSink()
    events: list[Event] = []
    first_response = tool_response()
    first_response.usage = LLMUsage(input_tokens=10, output_tokens=2)
    agent = make_agent(
        SequenceLLM([first_response]),
        RecordingTool(),
        events,
    )
    agent.tracer = Tracer(sink)

    with pytest.raises(IndexError):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    turn_span = sink.spans[-1]
    assert "input_tokens" not in turn_span.attributes
    assert "output_tokens" not in turn_span.attributes
    assert "usage" not in events[-1].data


def test_run_turn_preserves_success_when_trace_sink_fails() -> None:
    sink = FailingSink()
    events: list[Event] = []
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
        event_handler=lambda event: events.append(event),
    )

    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"
    assert agent.state.messages == [
        Message(role="user", content="hello"),
        Message(role="assistant", content="done"),
    ]
    assert sink.calls == 1
    trace_finish = [event for event in events if event.type == "trace_finish"]
    assert len(trace_finish) == 1
    assert trace_finish[0].data["persisted"] is False
    assert trace_finish[0].data["persistence_error"] == (
        "OSError: simulated trace write failure"
    )


def test_run_turn_marks_llm_and_root_traces_as_error() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)
    agent = Agent(
        llm=FailingLLM(),
        tools=ToolRegistry(),
        tracer=tracer,
    )

    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

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


def test_run_turn_preserves_llm_error_when_trace_sink_fails() -> None:
    sink = FailingSink()
    events: list[Event] = []
    agent = Agent(
        llm=FailingLLM(),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
        event_handler=lambda event: events.append(event),
    )

    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    assert agent.state.messages == []
    assert sink.calls == 1
    trace_finish = [event for event in events if event.type == "trace_finish"]
    assert len(trace_finish) == 1
    assert trace_finish[0].data["status"] == "error"
    assert trace_finish[0].data["persisted"] is False
    assert trace_finish[0].data["persistence_error"] == (
        "OSError: simulated trace write failure"
    )


@pytest.mark.parametrize("with_handler", [False, True])
def test_run_turn_ends_allowed_permission_span_once(with_handler: bool) -> None:
    sink = RecordingSink()
    llm = SequenceLLM([tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    agent.tracer = Tracer(sink)
    handler_calls: list[ToolCall] = []

    if with_handler:

        def allow(tool_call: ToolCall) -> PermissionResult:
            handler_calls.append(tool_call)
            return PermissionResult(
                policy_decision=PermissionDecision.ALLOW,
                allowed=True,
            )

        agent.permission_handler = allow

    assert (
        asyncio.run(run_turn(agent, "use the tool", budget=TEST_BUDGET)) == "finished"
    )
    assert tool.calls == [{"value": 42}]
    # A baseline operation inside the sandbox never asks for approval.
    assert handler_calls == []

    permission_spans = [span for span in sink.spans if span.name == "permission.check"]
    assert len(permission_spans) == 1
    permission_span = permission_spans[0]
    assert permission_span.status == SpanStatus.OK
    assert permission_span.attributes["allowed"] is True
    assert permission_span.attributes["tool_call_id"] == "call-1"
    assert permission_span.attributes["handler_configured"] is with_handler
    assert permission_span.attributes["policy_decision"] == "allow"
    assert permission_span.attributes["source"] == "baseline"
    assert permission_span.attributes["granted_capabilities"] == []


def test_run_turn_ends_permission_span_once_when_handler_raises() -> None:
    sink = RecordingSink()
    tool = NetworkRequestTool()
    agent = make_agent(SequenceLLM([network_tool_response()]), tool)
    agent.tracer = Tracer(sink)

    def fail(tool_call: ToolCall) -> PermissionResult:
        raise RuntimeError("permission failed")

    agent.permission_handler = fail

    with pytest.raises(RuntimeError, match="permission failed"):
        asyncio.run(run_turn(agent, "use the tool", budget=TEST_BUDGET))

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

    with pytest.raises(RunBudgetExceeded):
        asyncio.run(
            run_turn(
                agent,
                "keep going",
                budget=RunBudget(max_steps=1),
            )
        )

    span = sink.spans[-1]

    assert span.name == "agent.turn"
    assert span.status == SpanStatus.ERROR
    assert span.error is not None
