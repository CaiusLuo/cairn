"""Compaction across turns: reuse while the history prefix is unchanged.

The user-visible promise is that a long conversation is summarized once and then
keeps running, rather than paying for the same summary at the start of every
turn. These tests pin that reuse, and pin the safe fallback when the record can
no longer describe the conversation.
"""

import pytest

from cairn.core.compaction import (
    CompactionRecord,
    ReusableCompaction,
    StaleCompaction,
)
from cairn.core.models import Message


def user(content: str) -> Message:
    return Message(role="user", content=content)


def assistant(content: str) -> Message:
    return Message(role="assistant", content=content)


def record(retained_from: int, summary: str = "s") -> CompactionRecord:
    return CompactionRecord(
        retained_from=retained_from,
        summary=summary,
        folded_tokens=1_000,
        tokens_after=100,
    )


class TestCapture:
    def test_captures_the_prefix_the_record_replaces(self) -> None:
        history = [user("a"), assistant("b"), user("c")]

        captured = ReusableCompaction.capture(history, record(2))

        assert len(captured.fingerprint) == 2

    def test_rejects_a_record_reaching_past_the_conversation(self) -> None:
        with pytest.raises(StaleCompaction):
            ReusableCompaction.capture([user("a")], record(5))


class TestReuse:
    def test_reuses_while_the_prefix_is_unchanged(self) -> None:
        history = [user("a"), assistant("b")]
        captured = ReusableCompaction.capture(history, record(2))

        grown = [*history, user("c"), assistant("d")]

        assert captured.reusable_with(grown) is True
        assert captured.record_for(grown).summary == "s"

    def test_refuses_when_the_replaced_prefix_changed(self) -> None:
        captured = ReusableCompaction.capture([user("a"), assistant("b")], record(2))

        rewritten = [user("different"), assistant("b")]

        assert captured.reusable_with(rewritten) is False

    def test_refuses_when_the_history_is_shorter(self) -> None:
        captured = ReusableCompaction.capture([user("a"), assistant("b")], record(2))

        assert captured.reusable_with([user("a")]) is False

    def test_record_for_raises_instead_of_silently_returning_nothing(self) -> None:
        captured = ReusableCompaction.capture([user("a"), assistant("b")], record(2))

        with pytest.raises(StaleCompaction):
            captured.record_for([user("changed"), assistant("b")])

    def test_fingerprint_distinguishes_tool_identity(self) -> None:
        captured = ReusableCompaction.capture(
            [
                Message(role="tool", tool_call_id="c1", content="x"),
                Message(role="tool", tool_call_id="c2", content="x"),
            ],
            record(2),
        )

        swapped = [
            Message(role="tool", tool_call_id="c2", content="x"),
            Message(role="tool", tool_call_id="c1", content="x"),
        ]

        assert captured.reusable_with(swapped) is False
