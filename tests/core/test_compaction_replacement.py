"""Property tests for the compaction substitution itself.

The user-facing contract is that compaction *replaces* a settled range in place.
It must never move the summary to the end, mutate the conversation it read, or
leave a tool result whose call the summary swallowed.
"""

from typing import Any

import pytest

from cairn.core.compaction import (
    SUMMARY_PREFIX,
    CompactionRecord,
    ReplacementError,
    apply_replacement,
)
from cairn.core.context import ContextBudget, ContextBuilder, TokenCount
from cairn.core.models import Message, ToolCall


def user(content: str = "u") -> Message:
    return Message(role="user", content=content)


def assistant(content: str = "a") -> Message:
    return Message(role="assistant", content=content)


def tool_call(call_id: str) -> Message:
    return Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": "a"})],
    )


def tool_result(call_id: str, content: str = "x" * 50) -> Message:
    return Message(role="tool", tool_call_id=call_id, content=content)


def group(call_id: str) -> list[Message]:
    return [tool_call(call_id), tool_result(call_id)]


def conversation() -> list[Message]:
    return [
        user("task one"),
        *group("a"),
        assistant("first answer"),
        user("task two"),
        *group("b"),
        assistant("second answer"),
        user("current"),
    ]


def record(retained_from: int, summary: str = "s") -> CompactionRecord:
    """A record whose summary is deliberately shorter than what it replaces."""
    return CompactionRecord(
        retained_from=retained_from,
        summary=summary,
        folded_tokens=1000,
        tokens_after=100,
    )


class TestReplacementIsInPlace:
    def test_summary_occupies_the_start_of_the_replaced_range(self) -> None:
        history = conversation()
        edge = record(4)

        replaced = apply_replacement(history, edge)

        assert replaced[0].content is not None
        assert replaced[0].content.startswith(SUMMARY_PREFIX)
        # Everything after the substituted range keeps its own content order.
        assert replaced[1:] == history[4:]

    def test_summary_is_not_appended_to_the_end(self) -> None:
        history = conversation()
        edge = record(4)

        replaced = apply_replacement(history, edge)

        assert not (replaced[-1].content or "").startswith(SUMMARY_PREFIX)
        assert replaced[-1] == history[-1], "the current turn must remain last"

    def test_length_shrinks_by_the_replaced_prefix_minus_one(self) -> None:
        history = conversation()
        edge = record(4)

        replaced = apply_replacement(history, edge)

        assert len(replaced) == len(history) - 4 + 1

    def test_original_conversation_is_not_mutated(self) -> None:
        history = conversation()
        snapshot = [message.model_copy(deep=True) for message in history]

        apply_replacement(history, record(4))

        assert history == snapshot

    def test_replacement_never_orphans_a_tool_result(self) -> None:
        history = conversation()

        replaced = apply_replacement(history, record(4))

        results = {m.tool_call_id for m in replaced if m.role == "tool"}
        calls = {call.id for m in replaced for call in m.tool_calls}
        assert results <= calls

    def test_current_turn_survives_verbatim(self) -> None:
        history = conversation()

        replaced = apply_replacement(history, record(7))

        assert replaced[-1] == history[-1]

    def test_landing_on_a_tool_result_is_rejected(self) -> None:
        history = conversation()

        with pytest.raises(ReplacementError):
            apply_replacement(history, record(2))

    def test_split_that_orphans_a_group_is_rejected(self) -> None:
        history = conversation()

        # Index 2 is a tool result, so the survivors would lose its call.
        with pytest.raises(ReplacementError):
            apply_replacement(history, record(2))

    def test_a_record_that_replaces_nothing_cannot_be_built(self) -> None:
        # Rejecting at construction is stronger than rejecting at application:
        # no caller can construct "compacted" evidence for a no-op.
        with pytest.raises(ValueError):
            record(0)

    def test_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ReplacementError):
            apply_replacement(conversation(), record(99))


class SizeCounter:
    """Deterministic exact counter: one token per character."""

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            tokens=sum(len(message.content or "") for message in messages),
            is_estimate=False,
        )


class TestContextBuilderAppliesCompaction:
    def _builder(self, limit: int) -> ContextBuilder:
        return ContextBuilder(
            budget=ContextBudget(max_tokens=limit, response_tokens=10),
            counter=SizeCounter(),
        )

    def test_request_is_shorter_and_reports_the_substitution(self) -> None:
        history = conversation()
        builder = self._builder(limit=10_000)

        plain = builder.build(
            system_messages=[], history=history, current_turn_start=8, tools=[]
        )
        compacted = builder.build(
            system_messages=[],
            history=history,
            current_turn_start=8,
            tools=[],
            compaction=record(4),
        )

        assert compacted.compacted is True
        assert compacted.compacted_messages == 4
        assert compacted.tokens_after.tokens < plain.tokens_after.tokens
        assert compacted.compaction_tokens_saved > 0

    def test_history_is_untouched_by_the_builder(self) -> None:
        history = conversation()
        snapshot = [message.model_copy(deep=True) for message in history]

        self._builder(limit=10_000).build(
            system_messages=[],
            history=history,
            current_turn_start=8,
            tools=[],
            compaction=record(4),
        )

        assert history == snapshot

    def test_current_turn_stays_verbatim_in_the_request(self) -> None:
        history = conversation()

        request = self._builder(limit=10_000).build(
            system_messages=[],
            history=history,
            current_turn_start=8,
            tools=[],
            compaction=record(4),
        )

        assert request.messages[-1] == history[-1]

    def test_a_growthless_record_is_visible_to_the_caller(self) -> None:
        # A summary longer than the range it replaces must be detectable before
        # the caller keeps it; otherwise compaction can raise the request size.
        history = conversation()
        fat = record(4, summary="x" * 500)

        request = self._builder(limit=10_000).build(
            system_messages=[],
            history=history,
            current_turn_start=8,
            tools=[],
            compaction=fat,
        )

        assert request.compacted is True
        assert request.compaction_tokens_saved == 0, (
            "an oversized summary must not be reported as saving tokens"
        )

    def test_without_a_record_nothing_is_reported_as_compacted(self) -> None:
        request = self._builder(limit=10_000).build(
            system_messages=[],
            history=conversation(),
            current_turn_start=8,
            tools=[],
        )

        assert request.compacted is False
        assert request.compacted_messages == 0
        assert request.compaction_tokens_saved == 0
