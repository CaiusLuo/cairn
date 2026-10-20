"""Property tests for the compaction rules.

The rules are pure, so these tests assert invariants that must hold for every
message shape. Pinning exact boundaries would make the tests follow the
implementation instead of the contract, and the boundary choice is body-dependent
(how many tokens a message costs), not a fixed index.
"""

import pytest

from cairn.core.compaction import (
    ContextBudget,
    CutPointError,
    RetentionWindow,
    choose_boundary,
    count_groups,
    is_cut_point,
    legal_cut_points,
)
from cairn.core.compaction.rules import (
    CompactionPolicy,
    CompactionRecord,
    summarizer_reserve,
)
from cairn.core.context import TokenCount
from cairn.core.models import Message
from tests.support.compaction import assistant, group, tool_call, user


def window(budget: ContextBudget | None = None) -> RetentionWindow:
    return RetentionWindow.derive(budget or ContextBudget())


class TestCutPointInvariants:
    @pytest.mark.parametrize(
        "shape",
        [
            [user(), assistant()],
            [user(), *group("c1")],
            [user(), *group("c1"), assistant()],
            [user(), *group("c1"), *group("c2"), assistant()],
            [user(), *group("c1"), user("t2"), *group("c2")],
        ],
    )
    def test_no_legal_cut_starts_at_a_tool_result(self, shape: list[Message]) -> None:
        cuts = legal_cut_points(shape, upper=len(shape))

        for cut in cuts:
            assert shape[cut].role != "tool"

    @pytest.mark.parametrize(
        "shape",
        [
            [user(), *group("c1")],
            [user(), *group("c1"), *group("c2")],
        ],
    )
    def test_no_legal_cut_orphans_a_tool_result(self, shape: list[Message]) -> None:
        for cut in legal_cut_points(shape, upper=len(shape)):
            suffix = shape[cut:]
            results = {m.tool_call_id for m in suffix if m.role == "tool"}
            calls = {call.id for m in suffix for call in m.tool_calls}
            assert results <= calls, f"cut {cut} orphaned {results - calls}"

    def test_every_complete_group_end_is_a_legal_cut(self) -> None:
        shape = [user(), *group("c1"), assistant(), *group("c2")]

        # index 3 is the plain assistant that ends the first group; index 4 is
        # the tool result of the second group, which never starts a suffix.
        assert is_cut_point(shape, 3)
        assert not is_cut_point(shape, 4)

    def test_tool_call_assistant_is_never_a_cut_point(self) -> None:
        shape = [user(), *group("c1")]

        assert not is_cut_point(shape, 1)


class TestBoundaryInvariants:
    """Two turns of settled tool work followed by the current turn.

    A boundary needs something older than the newest group to replace, so every
    case here includes at least two settled groups before the current turn.
    """

    def settled(self) -> list[Message]:
        return [user("t1"), *group("a"), assistant("done a"), user("t2"), *group("b")]

    def test_retained_suffix_never_contains_an_orphan_tool_result(self) -> None:
        shape = self.settled()
        boundary = choose_boundary(shape, window=window(), turn_start=len(shape))

        suffix = shape[boundary.retained_from :]
        results = {m.tool_call_id for m in suffix if m.role == "tool"}
        calls = {call.id for m in suffix for call in m.tool_calls}
        assert results <= calls

    def test_split_never_reaches_the_current_turn(self) -> None:
        shape = self.settled()
        boundary = choose_boundary(shape, window=window(), turn_start=len(shape))

        assert boundary.retained_from <= len(shape)

    def test_split_never_lands_inside_a_group(self) -> None:
        shape = self.settled()
        boundary = choose_boundary(shape, window=window(), turn_start=len(shape))

        assert is_cut_point(shape, boundary.retained_from)

    def test_boundary_always_replaces_something(self) -> None:
        shape = self.settled()

        boundary = choose_boundary(shape, window=window(), turn_start=len(shape))

        assert boundary.retained_from > 0, "a compaction must replace something"
        assert boundary.replaced_count > 0

    def test_stricter_group_floor_retains_more(self) -> None:
        shape = [
            user("t1"),
            *group("a"),
            assistant("done a"),
            user("t2"),
            *group("b"),
            assistant("done b"),
            user("t3"),
            *group("c"),
        ]
        loose = RetentionWindow(tokens=100, minimum_groups=1)
        strict = RetentionWindow(tokens=100, minimum_groups=2)

        loose_boundary = choose_boundary(shape, window=loose, turn_start=len(shape))
        strict_boundary = choose_boundary(shape, window=strict, turn_start=len(shape))

        # A stricter floor can only retain at least as much, never less.
        assert strict_boundary.retained_from >= loose_boundary.retained_from
        assert count_groups(shape[strict_boundary.retained_from :]) >= 2

    def test_unreachable_group_floor_falls_back_instead_of_refusing(self) -> None:
        # Two settled groups cannot satisfy a floor of three. Refusing would mean
        # a longer conversation can never be compacted, so the floor is treated as
        # a preference and the earliest legal cut still applies.
        shape = self.settled()
        impossible = RetentionWindow(tokens=100, minimum_groups=3)

        boundary = choose_boundary(shape, window=impossible, turn_start=len(shape))

        assert boundary.retained_from > 0
        assert count_groups(shape[boundary.retained_from :]) < 3

    def test_plain_conversation_without_tool_groups_can_be_compacted(self) -> None:
        # The commonest shape of all: question, answer, question. A group floor
        # must not make it uncompactable.
        shape = [user("a"), assistant(), user("b"), assistant(), user("cur")]

        boundary = choose_boundary(shape, window=window(), turn_start=4)

        assert boundary.retained_from == 1

    def test_earliest_satisfying_split_is_chosen(self) -> None:
        shape = self.settled()

        boundary = choose_boundary(shape, window=window(), turn_start=len(shape))

        # The split must name the earliest legal point that keeps the floor, so
        # repeated compaction absorbs old turns instead of creeping forward.
        earlier = [
            cut
            for cut in legal_cut_points(shape, upper=len(shape))
            if cut > 0 and count_groups(shape[cut:]) >= 1
        ]
        assert boundary.retained_from == earlier[0]

    def test_raises_when_the_current_turn_has_no_history(self) -> None:
        with pytest.raises(CutPointError):
            choose_boundary([user("cur")], window=window(), turn_start=0)

    def test_group_count_matches_complete_interactions(self) -> None:
        shape = [user(), *group("a"), assistant(), *group("b")]

        assert count_groups(shape) == 2

    def test_incomplete_group_is_not_counted(self) -> None:
        shape = [user(), tool_call("a")]

        assert count_groups(shape) == 0


class TestRetentionWindow:
    def test_window_grows_with_the_budget(self) -> None:
        small = window(ContextBudget(max_tokens=16_000, response_tokens=1_000))
        large = window(ContextBudget(max_tokens=128_000, response_tokens=8_000))

        assert small.tokens < large.tokens

    def test_window_always_retains_a_group_floor(self) -> None:
        small = window(ContextBudget(max_tokens=4_000, response_tokens=1_000))

        assert small.minimum_groups >= 1

    def test_window_never_swallows_the_whole_budget(self) -> None:
        budget = ContextBudget(max_tokens=8_000, response_tokens=1_000)

        assert window(budget).tokens < budget.input_tokens


class TestCompactionPolicy:
    def _policy(self, budget: ContextBudget) -> CompactionPolicy:
        return CompactionPolicy(
            budget=budget, summarizer_reserve=summarizer_reserve(budget)
        )

    def test_threshold_follows_the_budget(self) -> None:
        small = self._policy(ContextBudget(max_tokens=8_000, response_tokens=1_000))
        large = self._policy(ContextBudget(max_tokens=64_000, response_tokens=1_000))

        assert small.threshold < large.threshold
        assert small.threshold == small.input_limit - small.summarizer_reserve

    @pytest.mark.parametrize("offset,expected", [(-1, False), (0, False), (1, True)])
    def test_threshold_boundary(self, offset: int, expected: bool) -> None:
        policy = self._policy(ContextBudget(max_tokens=8_000, response_tokens=1_000))

        assert (
            policy.should_compact(
                TokenCount(tokens=policy.threshold + offset, is_estimate=False)
            )
            is expected
        )

    def test_reserve_below_the_floor_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompactionPolicy(
                budget=ContextBudget(max_tokens=8_000, response_tokens=1_000),
                summarizer_reserve=10,
            )

    def test_a_budget_too_small_to_summarize_still_yields_a_policy(self) -> None:
        # A tiny budget must keep working: it folds by rule instead of failing at
        # the first tool batch, and its threshold stays at the input limit.
        policy = CompactionPolicy(
            budget=ContextBudget(max_tokens=4_000, response_tokens=1_000),
            summarizer_reserve=3_000,
        )

        assert policy.can_summarize is False
        assert policy.threshold == policy.input_limit

    def test_scaled_reserve_never_reaches_the_input_limit(self) -> None:
        budget = ContextBudget(max_tokens=3_000, response_tokens=1_000)

        assert summarizer_reserve(budget) < budget.input_tokens


class TestCompactionRecord:
    def test_rejects_a_blank_summary(self) -> None:
        with pytest.raises(ValueError):
            CompactionRecord(
                retained_from=1,
                summary="  ",
                folded_tokens=100,
                tokens_after=10,
            )

    def test_detects_a_compaction_that_grew_the_request(self) -> None:
        grown = CompactionRecord(
            retained_from=1,
            summary="s",
            folded_tokens=100,
            tokens_after=120,
        )
        shrank = CompactionRecord(
            retained_from=1,
            summary="s",
            folded_tokens=100,
            tokens_after=80,
        )

        assert grown.is_gainless()
        assert not shrank.is_gainless()
