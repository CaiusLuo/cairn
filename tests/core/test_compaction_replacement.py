"""Property tests for the compaction substitution itself.

The user-facing contract is that compaction *replaces* a settled range in place.
It must never move the summary to the end, mutate the conversation it read, or
leave a tool result whose call the summary swallowed.
"""

import pytest

from cairn.agent.context.compaction import (
    SUMMARY_PREFIX,
    CompactionRecord,
    ReplacementError,
    apply_replacement,
)
from cairn.agent.context.window import ContextBudget, ContextBuilder
from cairn.agent.models.messages import Message
from tests.support.compaction import CharacterCounter, assistant, tool_call, user
from tests.support.compaction import group as shared_group

#: This module's fixtures are sized so the summary is unambiguously shorter than
#: the range it replaces. The shared default result body is a single character,
#: which would make ``SUMMARY_PREFIX`` alone larger than the replaced prefix, so
#: every group here carries a body that is clearly worth summarizing.
RESULT_BODY = "x" * 50


def group(call_id: str) -> list[Message]:
    return shared_group(call_id, result_content=RESULT_BODY)


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


class TestContextBuilderAppliesCompaction:
    def _builder(self, limit: int) -> ContextBuilder:
        return ContextBuilder(
            budget=ContextBudget(max_tokens=limit, response_tokens=10),
            counter=CharacterCounter(),
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


def test_folding_view_preserves_current_turn_latest_results_and_history() -> None:
    history = [
        user("investigate"),
        *shared_group("old", result_content="x" * 2_000),
        *shared_group("latest", result_content="new evidence"),
    ]
    snapshot = [message.model_copy(deep=True) for message in history]
    folded = CompactionRecord(
        retained_from=0,
        summary="",
        folded_tokens=2_100,
        tokens_after=100,
        folded_results=(2,),
    )
    builder = ContextBuilder(
        budget=ContextBudget(max_tokens=10_000, response_tokens=100),
        counter=CharacterCounter(),
    )

    request = builder.build(
        system_messages=[],
        history=history,
        current_turn_start=0,
        tools=[],
        compaction=folded,
    )

    assert request.messages[0] == history[0]
    assert request.messages[-2:] == history[-2:]
    assert request.messages[2].content != history[2].content
    assert request.folded_tool_results == 1
    assert request.compacted_messages == 0
    assert not request.compacted, "rule folding is not a model-summary observation"
    assert request.tokens_after.tokens < 200
    assert history == snapshot


def test_folding_record_cannot_rewrite_a_user_or_a_tool_call() -> None:
    history = [user("keep"), tool_call("pending")]
    for index in (0, 1, 2):
        record = CompactionRecord(
            retained_from=0,
            summary="",
            folded_tokens=100,
            tokens_after=10,
            folded_results=(index,),
        )
        with pytest.raises(ReplacementError):
            apply_replacement(history, record)


def test_emergency_folding_counts_recorded_results_only_once() -> None:
    history = [
        user("task"),
        *shared_group("old", result_content="x" * 2_000),
        *shared_group("latest", result_content="y" * 2_000),
    ]
    record = CompactionRecord(
        retained_from=0,
        summary="",
        folded_tokens=4_000,
        tokens_after=2_100,
        folded_results=(2,),
    )
    request = ContextBuilder(
        budget=ContextBudget(max_tokens=500, response_tokens=100),
        counter=CharacterCounter(),
    ).build(
        system_messages=[],
        history=history,
        current_turn_start=0,
        tools=[],
        compaction=record,
    )

    assert request.folded_tool_results == 2
    assert request.tokens_after.tokens <= 400
    assert history[-1].content == "y" * 2_000


@pytest.mark.parametrize("summary_end", [0, 2])
def test_reused_fold_and_emergency_folding_really_omit_old_turns(
    summary_end: int,
) -> None:
    history = [
        user("old question" * 15),
        assistant("old answer" * 15),
        user("current"),
        *shared_group("a", result_content="x" * 1_000),
        *shared_group("b", result_content="y" * 1_000),
    ]
    record = CompactionRecord(
        retained_from=summary_end,
        summary="earlier facts" if summary_end else "",
        folded_tokens=3_000,
        tokens_after=1_500,
        folded_results=(4,),
    )
    request = ContextBuilder(
        budget=ContextBudget(max_tokens=800, response_tokens=100),
        counter=CharacterCounter(),
    ).build(
        system_messages=[],
        history=history,
        current_turn_start=2,
        tools=[],
        compaction=record,
    )

    assert request.omitted_turns
    assert request.folded_tool_results == 2
    assert not request.compacted, "an omitted summary was not used by this request"
    assert any(message.content == "current" for message in request.messages)
    assert not any(
        "old question" in (message.content or "") for message in request.messages
    )
    assert request.tokens_after.tokens <= 700
    assert history[-1].content == "y" * 1_000
