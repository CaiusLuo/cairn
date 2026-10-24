"""Behaviour of the two-stage compaction use case.

These tests exercise the decision order and the failure chain, which is where the
user-visible guarantees live: the common case costs no model call, and every
failure degrades to a usable view instead of ending the turn.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from cairn.core.compaction import CompactionStatus
from cairn.core.compaction.summarizer import FAILURE_EMPTY, SummaryResult
from cairn.core.context import ContextBudget
from cairn.core.models import Message
from cairn.runtime.turn.compaction import (
    CompactionInput,
    CompactionOutcome,
    CompactionStep,
    RequestCompactor,
    compact,
)
from tests.support.compaction import (
    CharacterCounter,
    StubSummarizer,
    assistant,
    tool_call,
    tool_result,
    user,
)


def history(groups: int = 2, size: int = 400) -> list[Message]:
    """Settled tool groups followed by the current turn.

    The tool name and arguments are incidental here: these cases never execute a
    tool, and only the call id pairs a call with its result.
    """
    messages: list[Message] = [user("first task")]
    for index in range(groups):
        messages.append(tool_call(f"c{index}", name="bash", arguments={"command": "x"}))
        messages.append(tool_result(f"c{index}", content="x" * size))
        messages.append(assistant(f"done {index}"))
    messages.append(user("current task"))
    return messages


def source(
    messages: list[Message], *, budget_tokens: int, turn_start: int | None = None
) -> CompactionInput:
    return CompactionInput(
        messages=messages,
        turn_start=(len(messages) - 1 if turn_start is None else turn_start),
        system_messages=[Message(role="system", content="sys")],
        tools=[],
    )


def compactor(budget_tokens: int, *, keep_groups: int = 1) -> RequestCompactor:
    return RequestCompactor(
        counter=CharacterCounter(),
        budget=ContextBudget(max_tokens=budget_tokens, response_tokens=100),
        keep_groups=keep_groups,
    )


def run(coro: Coroutine[Any, Any, CompactionOutcome]) -> CompactionOutcome:
    return asyncio.run(coro)


class TestDecisionOrder:
    def test_within_budget_makes_no_model_call(self) -> None:
        summarizer = StubSummarizer("summary")

        outcome = run(
            compact(
                source=source(history(), budget_tokens=100_000),
                compactor=compactor(100_000),
                summarizer=summarizer,
                summarize_budget=500,
            )
        )

        assert outcome.step is CompactionStep.NOT_NEEDED
        assert summarizer.calls == []
        assert outcome.record is None

    def test_folding_alone_avoids_the_summarizer(self) -> None:
        # The unmodified request is 837 tokens and the folded one is 500, while a
        # 1_800-token budget puts the threshold at 676: folding alone suffices.
        summarizer = StubSummarizer("summary")

        outcome = run(
            compact(
                source=source(history(), budget_tokens=1_800),
                compactor=compactor(1_800),
                summarizer=summarizer,
                summarize_budget=500,
            )
        )

        assert outcome.step is CompactionStep.FOLDED
        assert summarizer.calls == [], "folding that suffices must not call a model"
        # Two groups exist; the newest one is protected, so one body was folded.
        assert outcome.folded_messages == 1

    def test_summarizes_only_when_folding_is_insufficient(self) -> None:
        summarizer = StubSummarizer("a much shorter summary")
        messages = history(groups=6, size=2_000)

        outcome = run(
            compact(
                source=source(messages, budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=summarizer,
                summarize_budget=500,
            )
        )

        assert outcome.step is CompactionStep.SUMMARIZED
        assert summarizer.calls
        assert outcome.record is not None
        assert outcome.tokens_after < outcome.tokens_before

    def test_summarizer_never_receives_the_current_turn(self) -> None:
        summarizer = StubSummarizer("short")
        messages = history(groups=6, size=2_000)

        run(
            compact(
                source=source(messages, budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=summarizer,
                summarize_budget=500,
            )
        )

        sent = summarizer.calls[0][0]
        assert all(message.content != "current task" for message in sent)

    def test_summarizer_never_receives_a_partial_group(self) -> None:
        summarizer = StubSummarizer("short")
        messages = history(groups=6, size=2_000)

        run(
            compact(
                source=source(messages, budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=summarizer,
                summarize_budget=500,
            )
        )

        sent = summarizer.calls[0][0]
        results = {m.tool_call_id for m in sent if m.role == "tool"}
        calls = {call.id for m in sent for call in m.tool_calls}
        assert results <= calls


class TestFailureChain:
    def test_empty_summary_degrades_to_folding(self) -> None:
        outcome = run(
            compact(
                source=source(history(groups=6, size=2_000), budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=StubSummarizer(failure=FAILURE_EMPTY),
                summarize_budget=500,
            )
        )

        assert outcome.step is CompactionStep.FOLDED
        assert outcome.status is CompactionStatus.SUMMARIZER_FAILED
        assert outcome.record is None

    def test_model_error_degrades_to_folding(self) -> None:
        outcome = run(
            compact(
                source=source(history(groups=6, size=2_000), budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=StubSummarizer(error=RuntimeError("provider down")),
                summarize_budget=500,
            )
        )

        assert outcome.step is CompactionStep.FOLDED
        assert outcome.status is CompactionStatus.SUMMARIZER_FAILED
        assert outcome.record is None

    def test_summary_that_does_not_shrink_is_rejected(self) -> None:
        fat = "y" * 5_000

        outcome = run(
            compact(
                source=source(history(groups=6, size=2_000), budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=StubSummarizer(fat),
                summarize_budget=500,
            )
        )

        assert outcome.record is None, "a summary that grows the request is not kept"
        assert outcome.status is CompactionStatus.SUMMARIZER_FAILED

    def test_missing_summarizer_reports_exhaustion(self) -> None:
        outcome = run(
            compact(
                source=source(history(groups=6, size=2_000), budget_tokens=3_000),
                compactor=compactor(3_000),
                summarizer=None,
                summarize_budget=500,
            )
        )

        assert outcome.status is CompactionStatus.EXHAUSTED
        assert outcome.record is None

    def test_cancellation_propagates_without_a_record(self) -> None:
        summarizer = StubSummarizer(error=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            run(
                compact(
                    source=source(history(groups=6, size=2_000), budget_tokens=3_000),
                    compactor=compactor(3_000),
                    summarizer=summarizer,
                    summarize_budget=500,
                )
            )

    def test_failure_reason_is_the_fixed_code(self) -> None:
        result = SummaryResult.failed(FAILURE_EMPTY)

        assert result.failure == FAILURE_EMPTY
        assert result.ok is False


class TestFolding:
    def test_folding_keeps_every_message_in_place(self) -> None:
        messages = history(groups=3)
        request = compactor(100_000)

        folded = request.fold(messages, up_to=len(messages))

        assert len(folded) == len(messages)
        assert [m.role for m in folded] == [m.role for m in messages]
        assert folded[0] == messages[0]

    def test_folding_keeps_tool_call_pairing(self) -> None:
        messages = history(groups=3)

        folded = compactor(100_000, keep_groups=1).fold(messages, up_to=len(messages))

        results = {m.tool_call_id for m in folded if m.role == "tool"}
        calls = {call.id for m in folded for call in m.tool_calls}
        assert results <= calls

    def test_newest_group_keeps_its_full_output(self) -> None:
        messages = history(groups=3)

        folded = compactor(100_000, keep_groups=1).fold(messages, up_to=len(messages))

        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
        assert folded[tool_indices[-1]] == messages[tool_indices[-1]]
        assert folded[tool_indices[0]] != messages[tool_indices[0]]

    def test_placeholders_state_how_much_was_omitted(self) -> None:
        messages = history(groups=2, size=400)

        folded = compactor(100_000, keep_groups=1).fold(messages, up_to=len(messages))

        folded_text = next(
            m.content or "" for m in folded if "omitted" in (m.content or "")
        )
        assert "400 characters" in folded_text
