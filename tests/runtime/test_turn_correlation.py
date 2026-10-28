"""Turn spans carry session/turn/task correlation and trigger reasons.

This reuses the real ``run_turn`` path and a recording sink: no new tracing
system, no second loop. It checks that opaque ids reach the root span, that all
child spans share its trace, and that compaction/fallback reasons are fixed
classes (never provider text).
"""

import asyncio

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.models import LLMResponse
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.session import Session
from cairn.runtime.turn_trace import AttemptContext, TurnTrace
from tests.support.runtime import RecordingSink, SequenceLLM


def test_root_span_carries_session_and_turn_ids() -> None:
    sink = RecordingSink()
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
    )
    session = Session(agent)
    asyncio.run(
        run_turn(agent, "hello", budget=RunBudget(max_steps=3), session=session)
    )

    root = next(span for span in sink.spans if span.name == "agent.turn")
    assert root.attributes["session_id"] == str(session.session_id)
    assert "turn_id" in root.attributes
    assert "task_id" not in root.attributes  # stateless session has no task
    # Every child span shares the root's trace id.
    for span in sink.spans:
        assert span.context.trace_id == root.context.trace_id


def test_stateless_turn_has_no_correlation_ids() -> None:
    sink = RecordingSink()
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
    )
    asyncio.run(run_turn(agent, "hello", budget=RunBudget(max_steps=3)))
    root = next(span for span in sink.spans if span.name == "agent.turn")
    assert "session_id" not in root.attributes
    assert "turn_id" not in root.attributes


def test_correlation_ids_are_opaque_not_content() -> None:
    sink = RecordingSink()
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
    )
    session = Session(agent)
    asyncio.run(
        run_turn(
            agent,
            "read /etc/passwd now",
            budget=RunBudget(max_steps=3),
            session=session,
        )
    )
    root = next(span for span in sink.spans if span.name == "agent.turn")
    dumped = repr(root.attributes)
    assert "/etc/passwd" not in dumped
    assert "now" not in root.attributes.get("session_id", "")


def _request(builder: ContextBuilder):  # type: ignore[no-untyped-def]
    from cairn.core.models import Message

    return builder.build(
        system_messages=[Message(role="system", content="sys")],
        history=[Message(role="user", content="hi")],
        current_turn_start=0,
        tools=[],
    )


def test_fallback_attempt_records_a_fixed_reason() -> None:
    """A second candidate attempt is labelled; provider text never appears."""
    sink = RecordingSink()
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
    )
    builder = ContextBuilder(budget=ContextBudget(max_tokens=1000, response_tokens=100))
    trace = TurnTrace(agent, RunBudget(max_steps=3))
    trace.start()
    trace.begin_step(
        AttemptContext(step=1, tool_schema_count=0, system_message_count=1, memory=None)
    )
    trace.on_request(
        _request(builder),
        builder.budget,
        model=None,
        model_id=None,
        attempt_number=2,
    )
    trace.on_response(LLMResponse(content="ok"))  # ends the attempt span
    trace.close(None)
    attempt_span = next(span for span in sink.spans if span.name == "llm.generate")
    assert attempt_span.attributes["fallback_reason"] == "candidate_unavailable"
    assert attempt_span.attributes["fallback_attempt"] == 1


def test_first_attempt_has_no_fallback_reason() -> None:
    sink = RecordingSink()
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
    )
    trace = TurnTrace(agent, RunBudget(max_steps=3))
    trace.start()
    trace.begin_step(
        AttemptContext(step=1, tool_schema_count=0, system_message_count=0, memory=None)
    )
    trace.on_response(LLMResponse(content="ok"))
    trace.close(None)
    # Nothing labels a fallback on the root span either.
    root = next(span for span in sink.spans if span.name == "agent.turn")
    assert "fallback_reason" not in root.attributes
