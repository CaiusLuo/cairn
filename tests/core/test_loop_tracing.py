import asyncio
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

import cairn.terminal.output as ui
from cairn.core.agent import Agent
from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    TokenCount,
)
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.core.permissions import PermissionResult
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
    llm_spans = [span for span in sink.spans if span.name == "llm.generate"]
    assert len(llm_spans) == 2
    assert all(span.status == SpanStatus.OK for span in llm_spans)
    assert all(
        span.context.trace_id == turn_span.context.trace_id for span in llm_spans
    )
    assert all(
        span.context.parent_span_id == turn_span.context.span_id for span in llm_spans
    )
    assert events[0].type == "trace_start"
    assert events[-1].type == "trace_finish"
    assert events[-1].data["status"] == "ok"
    assert events[-1].data["usage"] == {
        "input_tokens": 30,
        "output_tokens": 5,
    }


def test_run_turn_marks_usage_unknown_when_later_llm_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = RecordingSink()
    events: list[Event] = []
    output = StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False, width=200),
    )
    first_response = tool_response()
    first_response.usage = LLMUsage(input_tokens=10, output_tokens=2)
    agent = make_agent(
        SequenceLLM([first_response]),
        RecordingTool(),
        events,
    )
    agent.tracer = Tracer(sink)

    def handle_event(event: Event) -> None:
        events.append(event)
        ui.console_event_handler(event)

    agent.event_handler = handle_event

    with pytest.raises(IndexError):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    turn_span = sink.spans[-1]
    assert "input_tokens" not in turn_span.attributes
    assert "output_tokens" not in turn_span.attributes
    assert "usage" not in events[-1].data
    assert (
        output.getvalue()
        .strip()
        .endswith(
            f"trace: {turn_span.context.trace_id} (error) "
            "· tokens: input unknown, output unknown"
        )
    )
    assert output.getvalue().count("tokens:") == 1


@pytest.mark.parametrize("completed_first_call", [False, True])
def test_run_turn_cancelled_llm_shows_unknown_usage_footer(
    monkeypatch: pytest.MonkeyPatch, completed_first_call: bool
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False, width=200),
    )

    class BlockingLLM(SequenceLLM):
        def __init__(self, responses: list[LLMResponse]) -> None:
            super().__init__(responses)
            self.started = asyncio.Event()

        async def generate(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
        ) -> LLMResponse:
            if self.responses:
                return await super().generate(messages, tools)
            self.calls.append((messages, tools))
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        sink = RecordingSink()
        events: list[Event] = []
        response = tool_response()
        response.usage = LLMUsage(input_tokens=10, output_tokens=2)
        llm = BlockingLLM([response] if completed_first_call else [])
        agent = make_agent(llm, RecordingTool())
        agent.tracer = Tracer(sink)

        def handle_event(event: Event) -> None:
            events.append(event)
            ui.console_event_handler(event)

        agent.event_handler = handle_event
        task = asyncio.create_task(run_turn(agent, "hello", budget=TEST_BUDGET))
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        turn_span = sink.spans[-1]
        assert turn_span.name == "agent.turn"
        assert turn_span.status == SpanStatus.ERROR
        assert "input_tokens" not in turn_span.attributes
        assert "output_tokens" not in turn_span.attributes
        llm_spans = [span for span in sink.spans if span.name == "llm.generate"]
        assert len(llm_spans) == (2 if completed_first_call else 1)
        assert llm_spans[-1].attributes["cancelled"] is True
        if completed_first_call:
            assert llm_spans[0].attributes["input_tokens"] == 10
            assert llm_spans[0].attributes["output_tokens"] == 2
        assert events[-1].type == "trace_finish"
        assert events[-1].data["status"] == "error"
        assert "usage" not in events[-1].data
        assert (
            output.getvalue()
            .strip()
            .endswith(
                f"trace: {turn_span.context.trace_id} (error) "
                "· tokens: input unknown, output unknown"
            )
        )
        assert output.getvalue().count("tokens:") == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("llm_fails", [False, True])
def test_run_turn_preserves_primary_result_when_trace_sink_fails(
    llm_fails: bool,
) -> None:
    sink = FailingSink()
    events: list[Event] = []
    agent = Agent(
        llm=FailingLLM() if llm_fails else SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
        event_handler=lambda event: events.append(event),
    )

    if llm_fails:
        with pytest.raises(RuntimeError, match="llm failed"):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
        assert agent.state.messages == []
        trace_finish = [event for event in events if event.type == "trace_finish"]
        assert trace_finish[0].data["status"] == "error"
    else:
        assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"
        assert agent.state.messages == [
            Message(role="user", content="hello"),
            Message(role="assistant", content="done"),
        ]
        trace_finish = [event for event in events if event.type == "trace_finish"]
        assert trace_finish[0].data["status"] == "ok"
    assert sink.calls == 1
    assert len(trace_finish) == 1
    assert trace_finish[0].data["persisted"] is False
    assert (
        trace_finish[0].data["persistence_error"]
        == "OSError: simulated trace write failure"
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


def test_admission_failure_after_tool_preserves_known_completed_call_usage() -> None:
    class ToolOverflowCounter:
        def count(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> TokenCount:
            return TokenCount(
                tokens=100 if any(m.role == "tool" for m in messages) else 10,
                is_estimate=False,
            )

    response = tool_response()
    response.usage = LLMUsage(input_tokens=10, output_tokens=2)
    llm = SequenceLLM([response])
    events: list[Event] = []
    tool = RecordingTool()
    agent = make_agent(llm, tool, events)
    agent.context_builder = ContextBuilder(
        budget=ContextBudget(max_tokens=100, response_tokens=20),
        counter=ToolOverflowCounter(),
    )
    sink = RecordingSink()
    agent.tracer = Tracer(sink)

    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(run_turn(agent, "question", budget=TEST_BUDGET))

    assert len(llm.calls) == 1
    assert tool.calls == [{"value": 42}]
    assert [m.role for m in agent.state.messages] == ["user", "assistant", "tool"]
    assert len([s for s in sink.spans if s.name == "llm.generate"]) == 1
    assert events[-1].data["status"] == "error"
    assert events[-1].data["usage"] == {"input_tokens": 10, "output_tokens": 2}
