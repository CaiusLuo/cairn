"""Compaction inside a real turn.

These tests use the ordinary loop with a scripted client and an injected
summarizer, so they pin the two guarantees the loop owns: compaction happens
after a settled tool batch and before the next request, and a compaction failure
never ends a turn.
"""

import asyncio
from typing import Any

from cairn.core.compaction.summarizer import SummaryResult
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.infra.observability.models import SpanStatus
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.turn.compaction import CompactionStep
from cairn.runtime.turn.hook import run_compaction
from tests.support.compaction import CharacterCounter, StubSummarizer
from tests.support.runtime import (
    TEST_BUDGET,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
)


class ExplodingSummarizer:
    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        raise RuntimeError("provider down")


class BigOutputTool(RecordingTool):
    name = "big"

    async def execute(
        self, arguments: dict[str, Any], *, context: Any = None
    ) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(stdout="x" * 900, exit_code=0)


def tool_then_finish() -> list[LLMResponse]:
    call = ToolCall(id="call-1", name="big", arguments={"value": 1})
    return [
        LLMResponse(content=None, tool_calls=[call]),
        LLMResponse(content="finished"),
    ]


def build_agent(
    responses: list[LLMResponse],
    *,
    summarizer: Any,
    budget_tokens: int,
    sink: Any = None,
) -> Agent:
    tools = ToolRegistry()
    tools.register_tool(BigOutputTool())
    return Agent(
        llm=SequenceLLM(responses),
        tools=tools,
        # A short prompt keeps the never-trimmable part small enough that the tool
        # output is what pushes the request over, which is the case under test.
        system_prompt="sys",
        context_builder=ContextBuilder(
            budget=ContextBudget(max_tokens=budget_tokens, response_tokens=100),
            counter=CharacterCounter(),
        ),
        tracer=None if sink is None else Tracer(sink),
        summarizer=summarizer,
    )


def test_tool_output_overflow_is_compacted_and_the_turn_finishes() -> None:
    summarizer = StubSummarizer()
    # The raw 900-character tool output exceeds the budget; the folded view does
    # not, so folding alone is enough and no summary request is needed.
    agent = build_agent(tool_then_finish(), summarizer=summarizer, budget_tokens=1_000)

    answer = asyncio.run(run_turn(agent, "run the tool", budget=TEST_BUDGET))

    assert answer == "finished"
    assert summarizer.calls == [], "folded output alone kept the request admitted"
    # The executed fact is preserved in the conversation, never rewritten.
    tool_facts = [m for m in agent.state.messages if m.role == "tool"]
    assert len(tool_facts) == 1
    assert len(tool_facts[0].content or "") > 900


def test_a_failing_summarizer_does_not_end_the_turn() -> None:
    agent = build_agent(
        tool_then_finish(), summarizer=ExplodingSummarizer(), budget_tokens=1_200
    )

    answer = asyncio.run(run_turn(agent, "run the tool", budget=TEST_BUDGET))

    assert answer == "finished"
    assert agent.compaction is None


def test_without_a_summarizer_compaction_never_runs() -> None:
    agent = build_agent(tool_then_finish(), summarizer=None, budget_tokens=100_000)

    answer = asyncio.run(run_turn(agent, "run the tool", budget=TEST_BUDGET))

    assert answer == "finished"
    assert agent.compaction is None


def test_compaction_is_recorded_on_the_turn_span_without_the_summary_text() -> None:
    sink = RecordingSink()
    agent = build_agent(
        tool_then_finish(),
        summarizer=StubSummarizer("SECRET-SUMMARY-TEXT"),
        budget_tokens=1_200,
        sink=sink,
    )

    asyncio.run(run_turn(agent, "run the tool", budget=TEST_BUDGET))

    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status is SpanStatus.OK
    assert turn_span.attributes["compaction_count"] == 1
    serialized = str(turn_span.attributes)
    assert "SECRET-SUMMARY-TEXT" not in serialized


def test_within_budget_turns_never_summarize() -> None:
    summarizer = StubSummarizer()
    agent = build_agent(
        tool_then_finish(), summarizer=summarizer, budget_tokens=100_000
    )

    asyncio.run(run_turn(agent, "run the tool", budget=TEST_BUDGET))

    assert summarizer.calls == []
    assert agent.compaction is None


def test_manual_compaction_reduces_the_view_and_is_reused_by_the_next_turn() -> None:
    summarizer = StubSummarizer("shortened history")
    agent = build_agent([], summarizer=summarizer, budget_tokens=100_000)
    # Long enough that a short summary genuinely shrinks the request; a tiny
    # conversation is refused by the gainless guard, which is correct.
    for index in range(3):
        agent.state.add_user_message(f"task {index} " + "x" * 200)
        agent.state.add_assistant_message(f"answer {index} " + "y" * 200)
    agent.state.add_user_message("current task")

    outcome = asyncio.run(run_compaction(agent))

    assert outcome.record is not None, "a manual compaction should summarize"
    assert agent.compaction is not None
    assert summarizer.calls

    # The history itself is untouched: compaction only changes the request view.
    assert any(
        (message.content or "").startswith("task 0") for message in agent.state.messages
    )


def test_manual_compaction_is_forced_even_inside_the_budget() -> None:
    summarizer = StubSummarizer("shortened history")
    agent = build_agent([], summarizer=summarizer, budget_tokens=1_000_000)
    for index in range(3):
        agent.state.add_user_message(f"task {index}")
        agent.state.add_assistant_message(f"answer {index}")
    agent.state.add_user_message("current task")

    outcome = asyncio.run(run_compaction(agent))

    assert outcome.step is not CompactionStep.NOT_NEEDED
    assert summarizer.calls


def test_manual_compaction_without_a_summarizer_reports_exhaustion() -> None:
    agent = build_agent([], summarizer=None, budget_tokens=1_000_000)
    for index in range(3):
        agent.state.add_user_message(f"task {index}")
        agent.state.add_assistant_message(f"answer {index}")
    agent.state.add_user_message("current task")

    outcome = asyncio.run(run_compaction(agent))

    assert outcome.record is None
    assert agent.compaction is None


class TestRepeatedManualCompaction:
    """Asking twice must cost once, and must not summarize a summary."""

    def _agent(self) -> tuple[Agent, StubSummarizer]:
        summarizer = StubSummarizer("shortened history")
        agent = build_agent([], summarizer=summarizer, budget_tokens=100_000)
        for index in range(3):
            agent.state.add_user_message(f"task {index} " + "x" * 200)
            agent.state.add_assistant_message(f"answer {index} " + "y" * 200)
        agent.state.add_user_message("current task")
        return agent, summarizer

    def test_repeating_without_new_content_does_not_call_the_model(self) -> None:
        agent, summarizer = self._agent()

        first = asyncio.run(run_compaction(agent))
        repeats = [asyncio.run(run_compaction(agent)) for _ in range(3)]

        assert first.record is not None
        assert len(summarizer.calls) == 1, "repeating must not repay the summary"
        assert all(item.step is CompactionStep.NOT_NEEDED for item in repeats)

    def test_repeating_keeps_reporting_the_existing_compaction(self) -> None:
        agent, _ = self._agent()
        first = asyncio.run(run_compaction(agent))

        repeat = asyncio.run(run_compaction(agent))

        assert repeat.record == first.record, (
            "a repeat must report the summary already in force"
        )

    def test_new_content_compacts_again(self) -> None:
        agent, summarizer = self._agent()
        asyncio.run(run_compaction(agent))
        agent.state.add_user_message("a new question " + "z" * 400)
        agent.state.add_assistant_message("a new answer " + "w" * 400)

        again = asyncio.run(run_compaction(agent))

        assert again.step is CompactionStep.SUMMARIZED
        assert len(summarizer.calls) == 2
