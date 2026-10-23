"""Reasoning is kept in full and shown in part.

The block is worth retaining — it records why a step was taken — and not worth
reading by default: it is long, it is provisional, and it competes with the answer
for the reader's attention. These tests pin the rule the display relies on: a
collapsed block shows a short summary and says how much it is withholding, and
expanding reveals it in bounded steps rather than all at once.
"""

import pytest

from cairn.terminal.reasoning import (
    COLLAPSED_REASONING_LINES,
    EXPANDED_REASONING_STEP,
    ReasoningView,
)


def test_a_collapsed_view_shows_only_the_summary() -> None:
    view = ReasoningView()
    assert view.visible_lines(total=100) == COLLAPSED_REASONING_LINES
    assert view.hidden_lines(total=100) == 100 - COLLAPSED_REASONING_LINES


def test_a_short_block_hides_nothing() -> None:
    """Nothing is withheld when the whole block already fits the summary."""
    view = ReasoningView()
    assert view.visible_lines(total=1) == 1
    assert view.hidden_lines(total=1) == 0


def test_an_empty_block_shows_nothing() -> None:
    view = ReasoningView()
    assert view.visible_lines(total=0) == 0
    assert view.hidden_lines(total=0) == 0


def test_the_default_summary_is_short() -> None:
    """The summary must not displace the answer being written."""
    assert COLLAPSED_REASONING_LINES <= 3


def test_expanding_reveals_a_bounded_step() -> None:
    """One expansion must not dump an entire chain of thought."""
    view = ReasoningView()
    view.expand()
    assert view.visible_lines(total=1000) == (
        COLLAPSED_REASONING_LINES + EXPANDED_REASONING_STEP
    )


def test_repeated_expansion_grows_by_whole_steps() -> None:
    view = ReasoningView()
    first = view.visible_lines(total=1000)
    view.expand()
    view.expand()
    third = view.visible_lines(total=1000)
    assert third - first == 2 * EXPANDED_REASONING_STEP


def test_expansion_stops_at_the_end_of_the_block() -> None:
    view = ReasoningView()
    for _ in range(50):
        view.expand()
    assert view.visible_lines(total=10) == 10
    assert view.hidden_lines(total=10) == 0


def test_expanding_sets_the_expanded_state() -> None:
    view = ReasoningView()
    assert not view.expanded
    view.expand()
    assert view.expanded


def test_collapsing_returns_to_the_summary() -> None:
    view = ReasoningView()
    view.expand()
    view.expand()
    view.collapse()
    assert not view.expanded
    assert view.visible_lines(total=1000) == COLLAPSED_REASONING_LINES


def test_the_hint_names_the_key_and_the_count() -> None:
    """A silent truncation reads as lost content; the hint says it is collapsed."""
    view = ReasoningView()
    hidden = view.hidden_lines(total=40)
    hint = view.note(hidden)

    assert str(hidden) in hint
    assert "Ctrl+O" in hint


def test_no_hint_when_nothing_is_withheld() -> None:
    view = ReasoningView()
    assert view.note(view.hidden_lines(total=1)) == ""


@pytest.mark.parametrize("raw", [-1])
def test_a_negative_summary_length_is_refused(raw: int) -> None:
    with pytest.raises(ValueError, match="collapsed_lines must not be negative"):
        ReasoningView(collapsed_lines=raw)


@pytest.mark.parametrize("raw", [0, -5])
def test_a_nonpositive_step_is_refused(raw: int) -> None:
    with pytest.raises(ValueError, match="expanded_step must be at least one line"):
        ReasoningView(expanded_step=raw)
