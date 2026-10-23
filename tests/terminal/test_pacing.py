"""Pacing streamed text so it reads as typing rather than as bursts.

The reader's comfort is the whole point of this module, so the pacing rule is
stated in the terms a reader would notice:

* text must not appear in countable jumps, which means admitting a frame well
  inside the interval where a gap becomes visible;
* a slow render must not be asked to keep up, because a repaint that overruns its
  own interval is what produces flicker;
* a fast machine must not be punished for being fast.

The second and third pull against each other, which is why the interval adapts to
how long painting actually took instead of being fixed. A fixed interval either
flickers on a slow host or wastes work on a fast one.

No setting is exposed: the numbers are derived from measured paint time, so a
slow terminal over a slow link slows down by itself instead of asking the user to
tune something.
"""

from cairn.terminal.pacing import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    MAXIMUM_FRAME_INTERVAL_SECONDS,
    MINIMUM_FRAME_INTERVAL_SECONDS,
    StreamPacer,
)


def test_the_first_paint_is_immediate() -> None:
    """Text must appear as soon as it exists, not one interval later."""
    assert StreamPacer().admit(0.0)


def test_a_delta_inside_the_interval_is_refused() -> None:
    pacer = StreamPacer(interval=0.1)
    assert pacer.admit(0.0)
    assert not pacer.admit(0.01)
    assert not pacer.admit(0.09)
    assert pacer.admit(0.1)


def test_many_fast_deltas_cost_bounded_repaints() -> None:
    """A burst of model deltas must not become a repaint per delta."""
    pacer = StreamPacer(interval=0.033)
    paints = sum(1 for step in range(1000) if pacer.admit(step / 1000))
    # One second at 33ms is at most 31 frames.
    assert paints <= 32


def test_the_default_interval_is_the_comfortable_middle() -> None:
    """30fps: fast enough that arrivals read as continuous, cheap enough remotely."""
    assert DEFAULT_FRAME_INTERVAL_SECONDS == 1.0 / 30.0


def test_the_adaptive_interval_never_goes_below_the_floor() -> None:
    """A fast paint must not drive the interval so low that it floods."""
    pacer = StreamPacer()
    pacer.record_paint(duration=0.0001)
    assert pacer.interval >= MINIMUM_FRAME_INTERVAL_SECONDS
    assert pacer.interval == MINIMUM_FRAME_INTERVAL_SECONDS


def test_the_adaptive_interval_never_exceeds_the_ceiling() -> None:
    """A pathologically slow paint must not stall output for ever."""
    pacer = StreamPacer()
    pacer.record_paint(duration=10.0)
    assert pacer.interval == MAXIMUM_FRAME_INTERVAL_SECONDS


def test_a_slow_paint_slows_the_interval_proportionally() -> None:
    """The rule is a multiple of measured paint time, so it tracks the host."""
    pacer = StreamPacer()
    pacer.record_paint(duration=0.02)
    # Ten times the paint cost, comfortably inside the bounds.
    assert pacer.interval == 0.2


def test_a_fast_host_is_left_at_the_floor_not_slowed() -> None:
    """Blocking at the floor already bounds repaints; the pacer must not pad it."""
    pacer = StreamPacer()
    for _ in range(5):
        pacer.record_paint(duration=0.0005)
    assert pacer.interval == MINIMUM_FRAME_INTERVAL_SECONDS


def test_the_floor_is_below_the_visible_gap_threshold() -> None:
    """Arrivals must land inside the interval where a jump becomes countable."""
    # ~20Hz is the point below which chunks read as discrete steps.
    assert MINIMUM_FRAME_INTERVAL_SECONDS <= 1.0 / 20.0


def test_the_default_sits_between_the_bounds() -> None:
    assert MINIMUM_FRAME_INTERVAL_SECONDS <= DEFAULT_FRAME_INTERVAL_SECONDS
    assert DEFAULT_FRAME_INTERVAL_SECONDS <= MAXIMUM_FRAME_INTERVAL_SECONDS


def test_reset_makes_the_next_paint_immediate() -> None:
    pacer = StreamPacer(interval=10.0)
    assert pacer.admit(0.0)
    assert not pacer.admit(0.1)
    pacer.reset()
    assert pacer.admit(0.2)


def test_a_nonpositive_interval_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="interval must be positive"):
        StreamPacer(interval=0)


def test_a_negative_paint_duration_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="duration must not be negative"):
        StreamPacer().record_paint(duration=-1.0)
