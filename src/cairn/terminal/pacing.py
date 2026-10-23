"""Pacing a character-by-character stream so it never flickers.

Streaming one character per model delta would repaint the pane hundreds of times
a second for text the reader cannot follow, and on a slow link that reads as
flicker rather than as typing. This module decides *when* a repaint is worth
doing, independently of what is being painted, so the pacing rule is testable
without a terminal.

The rule is a frame budget, not a per-character delay: a delta is admitted at most
once per interval, and any text that arrived in between is shown on the next
admitted frame. The reader therefore sees steady progress instead of bursts, and
the total number of repaints is bounded by ``1 / interval`` per second no matter
how fast the model produces text.
"""

from dataclasses import dataclass, field

#: Default repaint interval. 30 fps is smooth to the eye and cheap enough that a
#: remote terminal over a slow link is not flooded; the CLI's existing live region
#: already refreshes at 8 Hz, so this is the smooth end of what is affordable.
DEFAULT_FRAME_INTERVAL_SECONDS = 1.0 / 30.0


@dataclass(slots=True)
class StreamPacer:
    """Decide which streamed deltas are worth a repaint.

    ``now`` is passed in rather than read from the clock so a test can drive the
    schedule exactly. The pacer holds no text: it only answers "may this delta
    paint now?".
    """

    interval: float = DEFAULT_FRAME_INTERVAL_SECONDS
    _last_paint: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError("interval must be positive")

    def admit(self, now: float) -> bool:
        """Whether a paint may happen at ``now``.

        The first call always admits, so the reader sees text as soon as it
        exists instead of waiting out an interval that has no previous frame to
        measure from.
        """
        if self._last_paint is None:
            self._last_paint = now
            return True
        if now - self._last_paint < self.interval:
            return False
        self._last_paint = now
        return True

    def reset(self) -> None:
        """Forget the last frame, so the next admitted paint is immediate."""
        self._last_paint = None
