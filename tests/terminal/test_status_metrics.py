"""What the status line can report while a turn is still running.

Two defects motivated these tests, both visible to a user as "the token numbers
never appear":

* the context gauge was initialised and read but never assigned, so it could not
  render at any point in any turn;
* token totals were only taken from ``trace_finish``, which carries no usage when
  a turn fails, so a turn that timed out at step 7 showed nothing even though its
  first six steps had measured usage.

The contract pinned here is the fix: context figures are adopted from the request
that is about to be sent, so the gauge is live during the turn, and a step's own
measured usage is adopted as it arrives rather than only at the end.
"""

import pytest

from cairn.terminal import output as ui


def _display() -> ui.StreamingDisplay:
    return ui.StreamingDisplay()


def test_the_context_gauge_starts_unknown() -> None:
    display = _display()
    assert display._context_used is None
    assert display._context_limit is None


def test_adopting_a_context_measurement_makes_the_gauge_renderable() -> None:
    display = _display()

    display.observe_context(used=24_856, limit=128_000)

    assert display._context_used == 24_856
    assert display._context_limit == 128_000


def test_the_gauge_reports_the_fraction_it_was_given() -> None:
    display = _display()
    display.observe_context(used=64_000, limit=128_000)

    line = display._status_line().plain

    assert "ctx" in line
    assert "50%" in line


@pytest.mark.parametrize("limit", [0, None])
def test_a_zero_or_missing_limit_does_not_render_a_gauge(limit: int | None) -> None:
    """An unknown limit must omit the figure rather than imply a real ratio."""
    display = _display()
    display.observe_context(used=100, limit=limit)

    assert "ctx" not in display._status_line().plain


def test_a_step_usage_is_adopted_before_the_turn_finishes() -> None:
    """The running total must be visible mid-turn, not only after it ends."""
    display = _display()
    display._turn_started = 1.0

    display.observe_step_usage(input_tokens=1_000, output_tokens=200)

    assert display._input_tokens == 1_000
    assert display._output_tokens == 200


def test_step_usage_accumulates_across_steps() -> None:
    display = _display()

    display.observe_step_usage(input_tokens=1_000, output_tokens=200)
    display.observe_step_usage(input_tokens=2_000, output_tokens=300)

    assert display._input_tokens == 3_000
    assert display._output_tokens == 500


def test_a_step_without_usage_leaves_the_running_total_alone() -> None:
    """A failed step reports no usage; that must not erase what earlier steps measured."""
    display = _display()
    display.observe_step_usage(input_tokens=1_000, output_tokens=200)

    display.observe_step_usage(input_tokens=None, output_tokens=None)

    assert display._input_tokens == 1_000
    assert display._output_tokens == 200


def test_resetting_a_turn_clears_the_running_total_but_keeps_the_limit() -> None:
    """The budget is a property of the session; the usage is a property of the turn."""
    display = _display()
    display.observe_context(used=1_000, limit=128_000)
    display.observe_step_usage(input_tokens=500, output_tokens=100)

    display.reset()

    assert display._input_tokens == 0
    assert display._output_tokens == 0
    assert display._context_limit == 128_000
