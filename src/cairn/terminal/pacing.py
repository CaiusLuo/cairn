"""Pacing streamed text so it reads as typing rather than as bursts.

The reader's comfort is what this module is for, so the rule is stated in terms a
reader would notice:

* text must not arrive in countable jumps — a gap that long reads as stepping, not
  as typing;
* a slow paint must not be asked to keep up, because a repaint that overruns its
  own interval is exactly what produces flicker;
* a fast host must not be penalised for being fast.

The last two pull against each other, which is why the interval adapts to how long
painting actually took rather than being fixed. A fixed interval either flickers on
a slow host or wastes work on a fast one.

Nothing here is user-configurable on purpose. The numbers are derived from measured
paint time, so a slow terminal over a slow link slows itself down instead of asking
the reader to discover and tune a setting. That mirrors what Aider does
(``min_delay = min(max(render_time * 10, 1/20), 2)``) for the same reason.
"""

from dataclasses import dataclass, field

#: Fastest repaint interval. 30fps is the comfortable middle for text: fast enough
#: that arrivals read as continuous, and cheap enough that a remote terminal is not
#: flooded. The source of the limit is text, not motion — 60fps is a motion budget
#: and streamed text is not motion, so spending it here would buy nothing.
DEFAULT_FRAME_INTERVAL_SECONDS = 1.0 / 30.0

#: Fraction of the frame interval the pacer allows a paint to consume before it
#: slows itself down. Ten times the measured cost keeps a comfortable margin: the
#: paint finishes well inside its own interval, so no frame is ever cut short.
PAINT_COST_MULTIPLIER = 10.0

#: A paint may not be faster than this. Below it successive frames land inside the
#: interval where arrivals stop reading as separate steps at all, so any faster is
#: invisible work.
MINIMUM_FRAME_INTERVAL_SECONDS = DEFAULT_FRAME_INTERVAL_SECONDS

#: A paint may not be slower than this. A single pathological paint must not stall
#: output for ever; past this the reader is better served by steady slow progress
#: than by waiting for an ideal frame.
MAXIMUM_FRAME_INTERVAL_SECONDS = 2.0


@dataclass(slots=True)
class StreamPacer:
    """Decide which streamed deltas are worth a repaint.

    ``now`` is passed in rather than read from a clock, so a test can drive the
    schedule exactly. The pacer holds no text: it only answers "may this delta
    paint now?" and remembers how expensive painting turned out to be.
    """

    interval: float = DEFAULT_FRAME_INTERVAL_SECONDS
    _last_paint: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError("interval must be positive")

    def admit(self, now: float) -> bool:
        """Whether a paint may happen at ``now``.

        The first call always admits, so the reader sees text as soon as it exists
        instead of waiting out an interval that has no previous frame to measure
        from.
        """
        if self._last_paint is None:
            self._last_paint = now
            return True
        if now - self._last_paint < self.interval:
            return False
        self._last_paint = now
        return True

    def record_paint(self, *, duration: float) -> None:
        """Adapt the interval to how long the paint just took.

        A paint that overruns its interval is what the reader sees as flicker, so
        the interval is raised to leave room for the cost actually observed. The
        bounds keep a very fast or a very slow host from producing an absurd
        interval: fast is capped at the floor (faster is invisible), slow is capped
        at the ceiling (steadiness beats perfection).
        """
        if duration < 0:
            raise ValueError("duration must not be negative")
        self.interval = min(
            MAXIMUM_FRAME_INTERVAL_SECONDS,
            max(MINIMUM_FRAME_INTERVAL_SECONDS, duration * PAINT_COST_MULTIPLIER),
        )

    def reset(self) -> None:
        """Forget the last frame, so the next admitted paint is immediate."""
        self._last_paint = None
