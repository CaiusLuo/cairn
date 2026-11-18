"""Compaction inside a real turn.

These tests use the ordinary loop with a scripted client and an injected
summarizer, so they pin the guarantees the loop owns: compaction runs before any
request (including the first), its usage reaches the same ledger as ordinary
calls, a durable reopen reuses the persisted summary without paying twice, and a
compaction failure never ends a turn.
"""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.agent.budget import BudgetReason, RunBudget, RunBudgetExceeded
from cairn.agent.context.compaction import SUMMARY_PREFIX
from cairn.agent.context.compaction.summarizer import FAILURE_MODEL_ERROR, SummaryResult
from cairn.agent.context.window import ContextBudget, ContextBuilder, TokenCount
from cairn.agent.harness import Agent
from cairn.agent.models.messages import (
    LLMResponse,
    LLMUsage,
    Message,
    ToolCall,
    ToolResult,
)
from cairn.agent.session import codec, durable
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.models import CheckpointPayload, CheckpointReceipt
from cairn.agent.tools.registry import ToolRegistry
from cairn.agent.tracing.models import SpanStatus
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.turn.compaction import CompactionStep
from cairn.agent.turn.hook import run_compaction
from cairn.agent.turn.loop import run_turn
from tests.support.compaction import CharacterCounter, StubSummarizer
from tests.support.durable_sessions import create_session, resolution
from tests.support.runtime import (
    TEST_BUDGET,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
)
from tests.support.sessions import SESSION, store


class ExplodingSummarizer(StubSummarizer):
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


def _several_tool_responses() -> list[LLMResponse]:
    return [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id=f"call-{index}", name="big", arguments={"value": index})
            ],
        )
        for index in range(4)
    ] + [LLMResponse(content="finished")]


def test_first_turn_reuses_safe_folds_before_the_next_request() -> None:
    summarizer = StubSummarizer()
    agent = build_agent(
        _several_tool_responses(), summarizer=summarizer, budget_tokens=4_000
    )

    assert asyncio.run(run_turn(agent, "investigate", budget=TEST_BUDGET)) == "finished"

    assert isinstance(agent.llm, SequenceLLM)
    sent, _ = agent.llm.calls[-1]
    original_results = [
        message for message in agent.state.messages if message.role == "tool"
    ]
    sent_results = [message for message in sent if message.role == "tool"]
    assert all(len(message.content or "") > 900 for message in original_results)
    assert sent_results[-1] == original_results[-1]
    assert sum(len(message.content or "") for message in sent_results) < 1_500
    assert summarizer.calls == []


def test_later_rule_folding_preserves_an_accepted_summary() -> None:
    summarizer = StubSummarizer("verified earlier work")
    agent = build_agent(
        _several_tool_responses(), summarizer=summarizer, budget_tokens=4_000
    )
    for index in range(3):
        agent.state.add_user_message(f"task {index} " + "x" * 200)
        agent.state.add_assistant_message(f"answer {index} " + "y" * 200)
    first = asyncio.run(run_compaction(agent))
    assert first.record is not None
    assert first.record.summary

    asyncio.run(run_turn(agent, "continue", budget=TEST_BUDGET))

    assert agent.compaction is not None
    assert agent.compaction.record.summary == first.record.summary
    assert agent.compaction.record.folded_results
    assert len(summarizer.calls) == 1, "rule folding must reuse the accepted summary"
    assert isinstance(agent.llm, SequenceLLM)
    sent, _ = agent.llm.calls[-1]
    assert any((message.content or "").startswith(SUMMARY_PREFIX) for message in sent)
    assert sent[-1] == agent.state.messages[-2]


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


def _settled_turns(agent: Agent, turns: int = 3, size: int = 200) -> None:
    """Old, completed question/answer turns before the current request."""
    for index in range(turns):
        agent.state.add_user_message(f"task {index} " + "x" * size)
        agent.state.add_assistant_message(f"answer {index} " + "y" * size)


def test_summary_usage_reaches_both_trace_and_budget() -> None:
    """One ledger feeds the trace, the budget and (through it) Run metrics."""
    sink = RecordingSink()
    summarizer = StubSummarizer(
        "carry the constraint", usage=LLMUsage(input_tokens=500, output_tokens=50)
    )
    agent = build_agent(
        [
            LLMResponse(
                content="finished",
                usage=LLMUsage(input_tokens=30, output_tokens=5),
            )
        ],
        summarizer=summarizer,
        budget_tokens=1_200,
        sink=sink,
    )
    _settled_turns(agent)

    assert asyncio.run(run_turn(agent, "continue", budget=TEST_BUDGET)) == "finished"

    assert len(summarizer.calls) == 1
    # Compaction ran before the first request, so the summary is already in the
    # view the model sees instead of a whole-turn omission notice.
    assert isinstance(agent.llm, SequenceLLM)
    sent, _ = agent.llm.calls[0]
    assert any((message.content or "").startswith(SUMMARY_PREFIX) for message in sent)

    usage = agent.last_budget_usage
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert usage is not None
    assert usage.input_tokens == 530
    assert usage.output_tokens == 55
    assert usage.total_tokens == 585
    assert turn_span.attributes["input_tokens"] == usage.input_tokens
    assert turn_span.attributes["output_tokens"] == usage.output_tokens


@pytest.mark.parametrize(
    "budget",
    [
        RunBudget(5, max_tokens=500),
        RunBudget(
            5, max_cost=0.0005, input_cost_per_million=1, output_cost_per_million=1
        ),
    ],
)
def test_unaffordable_summary_is_skipped_to_leave_room_for_the_answer(
    budget: RunBudget,
) -> None:
    summarizer = StubSummarizer()
    agent = build_agent(
        [LLMResponse(content="done", usage=LLMUsage(input_tokens=30, output_tokens=5))],
        summarizer=summarizer,
        budget_tokens=1_200,
    )
    _settled_turns(agent)

    assert asyncio.run(run_turn(agent, "continue", budget=budget)) == "done"
    assert summarizer.calls == []
    assert agent.last_budget_usage is not None
    assert agent.last_budget_usage.total_tokens == 35


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (RunBudget(5, max_tokens=1_200), BudgetReason.MAX_TOKENS),
        (
            RunBudget(
                5, max_cost=0.0012, input_cost_per_million=1, output_cost_per_million=1
            ),
            BudgetReason.MAX_COST,
        ),
    ],
)
def test_summary_spend_stops_the_turn_before_the_next_paid_request(
    budget: RunBudget,
    reason: BudgetReason,
) -> None:
    sink = RecordingSink()
    # The provider reports more input than the local estimate at dispatch.
    summarizer = StubSummarizer(
        "carry the constraint", usage=LLMUsage(input_tokens=1_250, output_tokens=50)
    )
    agent = build_agent(
        [LLMResponse(content="never sent")],
        summarizer=summarizer,
        budget_tokens=1_200,
        sink=sink,
    )
    _settled_turns(agent)

    with pytest.raises(RunBudgetExceeded) as error:
        asyncio.run(run_turn(agent, "continue", budget=budget))

    assert error.value.reason is reason
    assert len(summarizer.calls) == 1
    assert isinstance(agent.llm, SequenceLLM)
    assert agent.llm.calls == []
    usage = agent.last_budget_usage
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert usage is not None
    assert usage.total_tokens == 1_300
    assert usage.stop_reason == reason.value
    assert turn_span.attributes["input_tokens"] == 1_250
    assert turn_span.attributes["output_tokens"] == 50


def test_summary_is_booked_even_if_measuring_the_replacement_fails() -> None:
    class FailingCounter(CharacterCounter):
        def count(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> TokenCount:
            if any(
                (message.content or "").startswith(SUMMARY_PREFIX)
                for message in messages
            ):
                raise ValueError("cannot count replacement")
            return super().count(messages, tools)

    summarizer = StubSummarizer(
        "keep", usage=LLMUsage(input_tokens=500, output_tokens=50)
    )
    agent = build_agent(
        [LLMResponse(content="done", usage=LLMUsage(input_tokens=30, output_tokens=5))],
        summarizer=summarizer,
        budget_tokens=1_200,
    )
    agent.context_builder.counter = FailingCounter()
    _settled_turns(agent)
    assert asyncio.run(run_turn(agent, "continue", budget=TEST_BUDGET)) == "done"
    assert len(summarizer.calls) == 1
    assert agent.last_budget_usage is not None
    assert agent.last_budget_usage.total_tokens == 585
    assert agent.compaction is None


def test_failed_summary_usage_is_unknown_not_zero() -> None:
    sink = RecordingSink()
    summarizer = StubSummarizer(failure=FAILURE_MODEL_ERROR)
    agent = build_agent(
        [
            LLMResponse(
                content="finished",
                usage=LLMUsage(input_tokens=30, output_tokens=5),
            )
        ],
        summarizer=summarizer,
        budget_tokens=1_200,
        sink=sink,
    )
    _settled_turns(agent)

    with pytest.raises(RunBudgetExceeded) as error:
        asyncio.run(
            run_turn(agent, "continue", budget=RunBudget(5, max_tokens=1_000_000))
        )

    assert error.value.reason is BudgetReason.USAGE_UNKNOWN
    assert isinstance(agent.llm, SequenceLLM)
    assert agent.llm.calls == []
    assert len(summarizer.calls) == 1, "the failed attempt must still be visible"
    usage = agent.last_budget_usage
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert usage is not None
    assert usage.total_tokens is None
    assert usage.stop_reason == "usage_unknown"
    assert "input_tokens" not in turn_span.attributes
    assert "output_tokens" not in turn_span.attributes


def test_a_summary_that_finishes_during_cancellation_still_counts() -> None:
    """A paid summary request must not vanish because the turn was cancelled."""

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingSummarizer(StubSummarizer):
            async def summarize(
                self, messages: list[Message], *, max_tokens: int
            ) -> SummaryResult:
                started.set()
                try:
                    await asyncio.Future[None]()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return await super().summarize(messages, max_tokens=max_tokens)

        summarizer = BlockingSummarizer(
            "carried over", usage=LLMUsage(input_tokens=500, output_tokens=50)
        )
        agent = build_agent(
            [LLMResponse(content="never sent")],
            summarizer=summarizer,
            budget_tokens=1_200,
        )
        _settled_turns(agent)

        running = asyncio.create_task(run_turn(agent, "continue", budget=TEST_BUDGET))
        await asyncio.wait_for(started.wait(), 2)
        running.cancel("stop")
        try:
            await asyncio.wait_for(cancelled.wait(), 2)
            assert not running.done(), "rollback awaits the provider's cleanup"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running

        usage = agent.last_budget_usage
        assert usage is not None
        assert usage.total_tokens == 550
        assert isinstance(agent.llm, SequenceLLM)
        assert agent.llm.calls == [], "no provider request followed the summary"

    asyncio.run(scenario())


def test_cancelling_measurement_never_starts_a_paid_summary() -> None:
    async def scenario() -> None:
        measuring = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        class BlockingCounter(CharacterCounter):
            def count(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> TokenCount:
                loop.call_soon_threadsafe(measuring.set)
                if not release.wait(5):
                    raise TimeoutError("measurement was not released")
                return super().count(messages, tools)

        summarizer = StubSummarizer()
        agent = build_agent([], summarizer=summarizer, budget_tokens=1_200)
        agent.context_builder.counter = BlockingCounter()
        _settled_turns(agent)
        running = asyncio.create_task(run_turn(agent, "continue", budget=TEST_BUDGET))
        try:
            await asyncio.wait_for(measuring.wait(), 2)
            running.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert summarizer.calls == []
        assert agent.last_budget_usage is not None
        assert agent.last_budget_usage.requests == 0

    asyncio.run(scenario())


def _commit_a_summarized_turn(
    tmp_path: Path,
    *,
    policy_version: int = 1,
    before_summary_commit: Callable[[], None] | None = None,
) -> tuple[Any, StubSummarizer]:
    """Build a session whose latest checkpoint carries an accepted summary.

    Compaction is forced manually between committed turns, which is the host
    path for asking for a reduction at any time. The following turn commits the
    same record; the large context budget keeps automatic compaction out of the
    way so the test observes persistence, not a fresh summary.
    """
    summarizer = StubSummarizer(
        "KEEP: the original constraint",
        usage=LLMUsage(input_tokens=500, output_tokens=50),
        policy_version=policy_version,
    )
    agent = build_agent(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="seed-tool", name="big", arguments={"z": 1, "a": 2})
                ]
            ),
            LLMResponse(content="answer 0"),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="latest-tool", name="big", arguments={"value": 1})
                ]
            ),
            *[LLMResponse(content=f"answer {index}") for index in range(1, 4)],
        ],
        summarizer=summarizer,
        budget_tokens=100_000,
    )
    api, session = create_session(tmp_path, agent)
    try:
        for index in range(3):
            asyncio.run(
                run_turn(
                    agent,
                    f"task {index} " + "x" * 200,
                    budget=TEST_BUDGET,
                    session=session,
                )
            )
        outcome = asyncio.run(run_compaction(agent))
        assert outcome.record is not None
        if before_summary_commit is not None:
            before_summary_commit()
        assert (
            asyncio.run(
                run_turn(
                    agent, "commit the summary", budget=TEST_BUDGET, session=session
                )
            )
            == "answer 3"
        )
        saved = api.inspect(SESSION)
        if before_summary_commit is None:
            assert saved.checkpoint.compaction is not None
            assert (
                saved.checkpoint.compaction.summary == "KEEP: the original constraint"
            )
    finally:
        session.close()
    return saved, summarizer


def test_checkpoint_overflow_drops_only_the_summary_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encode = codec.encode_storage_checkpoint

    def encode_near_limit(
        checkpoint: CheckpointPayload,
        predecessor: CheckpointReceipt | None,
        *,
        format_version: int,
        sensitive_values: tuple[str, ...] = (),
    ) -> tuple[bytes, str]:
        if checkpoint.compaction is not None:
            plain = checkpoint.model_copy(update={"compaction": None})
            raw, _ = codec.encode_checkpoint(plain, sensitive_values=sensitive_values)
            monkeypatch.setattr(codec, "MAX_CHECKPOINT_BYTES", len(raw))
        return encode(
            checkpoint,
            predecessor,
            format_version=format_version,
            sensitive_values=sensitive_values,
        )

    def before_summary_commit() -> None:
        monkeypatch.setattr(durable, "encode_storage_checkpoint", encode_near_limit)

    saved, _ = _commit_a_summarized_turn(
        tmp_path, before_summary_commit=before_summary_commit
    )
    assert saved.checkpoint.compaction is None
    assert saved.checkpoint.messages[-1].content == "answer 3"


def test_durable_reopen_reuses_the_persisted_summary(tmp_path: Path) -> None:
    saved, _ = _commit_a_summarized_turn(tmp_path)
    cached = saved.checkpoint.compaction
    assert cached is not None
    assert any(
        call.id == "seed-tool"
        for message in saved.checkpoint.messages[: cached.retained_from]
        for call in message.tool_calls
    )
    reuse = StubSummarizer("MUST NOT BE USED")
    fresh = build_agent(
        [
            LLMResponse(
                content="second", usage=LLMUsage(input_tokens=10, output_tokens=5)
            )
        ],
        summarizer=reuse,
        budget_tokens=100_000,
    )
    reopened = DurableSession.reopen(
        store(tmp_path), SESSION, agent=fresh, binding=saved.binding
    )
    try:
        assert fresh.compaction is not None
        report = reopened.recovery_report
        reopened.reconcile(report, host_resolution=resolution(report))

        assert (
            asyncio.run(
                run_turn(fresh, "and now?", budget=TEST_BUDGET, session=reopened)
            )
            == "second"
        )

        assert reuse.calls == [], "the restored summary must not be paid for again"
        assert fresh.compaction is not None
        assert fresh.compaction.record.summary == "KEEP: the original constraint"
        assert isinstance(fresh.llm, SequenceLLM)
        sent, _ = fresh.llm.calls[0]
        rendered = "\n".join(
            message.content or "" for message in sent if message.role == "user"
        )
        assert "KEEP: the original constraint" in rendered
    finally:
        reopened.close()


def test_reopen_drops_a_summary_written_under_an_older_policy(
    tmp_path: Path,
) -> None:
    saved, _ = _commit_a_summarized_turn(tmp_path)
    newer = StubSummarizer("new policy summary", policy_version=2)
    fresh = build_agent(
        [
            LLMResponse(
                content="second", usage=LLMUsage(input_tokens=10, output_tokens=5)
            )
        ],
        summarizer=newer,
        budget_tokens=100_000,
    )
    reopened = DurableSession.reopen(
        store(tmp_path), SESSION, agent=fresh, binding=saved.binding
    )
    try:
        assert fresh.compaction is None, "an old policy must not be reused"
        report = reopened.recovery_report
        reopened.reconcile(report, host_resolution=resolution(report))

        assert (
            asyncio.run(
                run_turn(fresh, "and now?", budget=TEST_BUDGET, session=reopened)
            )
            == "second"
        )

        assert isinstance(fresh.llm, SequenceLLM)
        sent, _ = fresh.llm.calls[0]
        rendered = "\n".join(
            message.content or "" for message in sent if message.role == "user"
        )
        assert "KEEP: the original constraint" not in rendered
        assert newer.calls == [], "the huge budget needs no automatic summary"
    finally:
        reopened.close()
