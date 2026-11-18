import asyncio

import pytest

from cairn.agent.models.messages import LLMResponse, LLMUsage
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.turn.compaction import CompactionOutcome, CompactionStep
from cairn.agent.turn.events import Event
from cairn.agent.turn.loop import run_turn
from cairn.agent.turn.trace import TurnTrace
from tests.support.runtime import (
    TEST_BUDGET,
    FailingLLM,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    tool_response,
)


def test_turn_phase_events_cover_context_model_permission_and_tool() -> None:
    events: list[Event] = []
    first = tool_response()
    first.usage = LLMUsage(input_tokens=10, output_tokens=2)
    agent = make_agent(
        SequenceLLM(
            [
                first,
                LLMResponse(
                    content="done",
                    usage=LLMUsage(input_tokens=20, output_tokens=3),
                ),
            ]
        ),
        RecordingTool(),
        events,
    )
    sink = RecordingSink()
    agent.tracer = Tracer(sink)

    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"

    requests = [event for event in events if event.type == "model_request"]
    assert len(requests) == 2
    assert requests[0].data["attempt"] == 1
    assert requests[0].data["step"] == 1
    assert requests[0].data["context_prepare_ms"] >= 0
    assert requests[0].data["context_tokens_before"] >= 0

    responses = [event for event in events if event.type == "model_response"]
    assert [response.data["tool_call_count"] for response in responses] == [1, 0]
    assert all(response.data["duration_ms"] >= 0 for response in responses)
    assert responses[0].data["input_tokens"] == 10
    assert responses[1].data["output_tokens"] == 3

    permission = next(event for event in events if event.type == "permission_check")
    assert permission.data["tool_call_id"] == "call-1"
    assert permission.data["allowed"] is True
    assert permission.data["policy_decision"] == "allow"
    assert permission.data["duration_ms"] >= 0

    result = next(event for event in events if event.type == "tool_result")
    assert result.data["tool_call_id"] == "call-1"
    assert result.data["exit_code"] == 0
    assert result.data["duration_ms"] >= 0
    assert result.data["stdout_length"] == len("recorded")

    finish = next(event for event in events if event.type == "trace_finish")
    assert finish.data["persisted"] is True
    assert finish.data["turn_ms"] >= 0

    kinds = [event.type for event in events]
    assert kinds.index("tool_call") < kinds.index("permission_check")
    assert kinds.index("permission_check") < kinds.index("tool_result")


def test_failed_attempt_reports_its_type_and_duration_without_provider_text() -> None:
    events: list[Event] = []
    agent = make_agent(FailingLLM(), events=events)

    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))

    failure = next(event for event in events if event.type == "model_error")
    assert failure.data["error_type"] == "RuntimeError"
    assert failure.data["cancelled"] is False
    assert failure.data["duration_ms"] >= 0
    assert "llm failed" not in str(failure.data)


def test_compaction_phase_event_reports_duration_and_usage() -> None:
    events: list[Event] = []
    agent = make_agent(SequenceLLM([]), events=events)
    trace = TurnTrace(agent, TEST_BUDGET)

    trace.on_compaction(
        CompactionOutcome(
            step=CompactionStep.SUMMARIZED,
            folded_messages=4,
            tokens_before=900,
            tokens_after=300,
            summary_usage=LLMUsage(input_tokens=40, output_tokens=10),
        ),
        duration_ms=25.25,
    )

    compacted = next(event for event in events if event.type == "context_compacted")
    assert compacted.data == {
        "step": "summarized",
        "status": "ok",
        "folded_messages": 4,
        "tokens_before": 900,
        "tokens_after": 300,
        "duration_ms": 25.25,
        "input_tokens": 40,
        "output_tokens": 10,
    }
