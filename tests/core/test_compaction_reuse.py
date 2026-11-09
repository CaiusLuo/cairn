"""Compaction across turns: reuse while the history prefix is unchanged.

The user-visible promise is that a long conversation is summarized once and then
keeps running, rather than paying for the same summary at the start of every
turn. These tests pin that reuse, and pin the safe fallback when the record can
no longer describe the conversation.
"""

import pytest

from cairn.agent.context.compaction import (
    CompactionRecord,
    ReusableCompaction,
    StaleCompaction,
)
from cairn.agent.models.messages import Message
from tests.support.compaction import group


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


def test_fold_reuse_checks_the_result_and_the_call_arguments() -> None:
    history = [user("task"), *group("old", result_content="x" * 500)]
    record = CompactionRecord(
        retained_from=0,
        summary="",
        folded_tokens=600,
        tokens_after=100,
        folded_results=(2,),
    )
    captured = ReusableCompaction.capture(history, record)

    assert captured.reusable_with([*history, *group("new")])
    for index, changed in (
        (2, history[2].model_copy(update={"content": "different"})),
        (1, history[1].model_copy(update={"tool_calls": []})),
        (
            1,
            history[1].model_copy(
                update={
                    "tool_calls": [
                        history[1]
                        .tool_calls[0]
                        .model_copy(update={"arguments": {"path": "other"}})
                    ]
                }
            ),
        ),
    ):
        rewritten = list(history)
        rewritten[index] = changed
        assert not captured.reusable_with(rewritten)
    assert not captured.reusable_with(history[:2])


def test_fold_reuse_rejects_truncation_of_the_protected_suffix() -> None:
    history = [
        user("task"),
        *group("old", result_content="x" * 500),
        *group("latest", result_content="new evidence"),
    ]
    record = CompactionRecord(
        retained_from=0,
        summary="",
        folded_tokens=600,
        tokens_after=100,
        folded_results=(2,),
    )
    captured = ReusableCompaction.capture(history, record)

    assert not captured.reusable_with(history[:3])
    with pytest.raises(StaleCompaction):
        captured.record_for(history[:3])
