"""The reasoning preview, which lives only while a turn runs.

Reasoning is worth watching while it happens and worth nothing afterwards: a reader
who wants the argument wants the answer, and a reader who wants the evidence wants
the trace. So the preview is temporary by design, and the only thing it leaves
behind is a receipt saying how long it took.

That is the difference this module exists to enforce. The earlier behaviour
reprinted the whole block into the scrollback when the turn settled, which cost a
screenful of text per turn and left a `Ctrl+O` hint that could no longer do
anything. Both are consequences of treating temporary text as permanent.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

#: Rendered rows kept in the preview. Two is enough to show what the model is
#: doing and small enough that the answer keeps the screen.
PREVIEW_ROWS = 2

#: The animation frames, all one character wide so the gutter never shifts. A
#: multi-cell glyph would misalign on a terminal that renders it wide.
FRAMES = (".", "*", "+", "*")

#: Seconds between frames. Four per second is legible and cheap; anything faster
#: reads as flicker and costs a repaint over a slow link for no added signal.
FRAME_INTERVAL_SECONDS = 0.25


@dataclass(slots=True)
class ThinkingPreview:
    """The two-row window onto reasoning that is still arriving.

    The window follows the *newest* rows. Showing the opening rows was the earlier
    behaviour and it aged badly: by the time a reader looked, the preview had been
    describing the start of a thought the model had already moved past.
    """

    rows: int = PREVIEW_ROWS
    #: Extra rows one expansion adds. A whole step keeps the reveal predictable
    #: rather than moving the window one line at a time.
    expanded_rows: int = PREVIEW_ROWS * 3
    _lines: list[str] = field(default_factory=list, repr=False)
    _started: float | None = field(default=None, repr=False)
    _finished: float | None = field(default=None, repr=False)
    _frame: int = field(default=0, repr=False)
    _expanded: bool = field(default=False, repr=False)
    _revealed: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.rows < 1:
            raise ValueError("rows must be at least one")

    def begin(self, *, at: float) -> None:
        """Start a new preview, discarding anything left from the last one."""
        self._lines = []
        self._started = at
        self._finished = None
        self._frame = 0
        self._expanded = False
        self._revealed = 0

    def append(self, text: str) -> None:
        """Add newly arrived reasoning."""
        if not text:
            return
        self._lines.extend(text.splitlines())

    def finish(self, *, at: float) -> None:
        """Stop the preview. The text is not kept once the turn settles."""
        self._finished = at

    @property
    def running(self) -> bool:
        """Whether the preview is still being written.

        Only a running preview animates and offers its key hint; a finished one has
        nothing left to reveal, so advertising the key would be a lie.
        """
        return self._started is not None and self._finished is None

    @property
    def elapsed(self) -> float:
        """Seconds the reasoning took, or has been running."""
        if self._started is None:
            return 0.0
        end = self._finished if self._finished is not None else self._started
        return max(0.0, end - self._started)

    def has_content(self) -> bool:
        """Whether any reasoning arrived, so a receipt is worth printing.

        A turn that produced no reasoning gets no receipt: an empty "Thinking 0.0s"
        would claim a step that never happened.
        """
        return bool(self._lines)

    def advance(self) -> None:
        """Move to the next animation frame."""
        self._frame = (self._frame + 1) % len(FRAMES)

    @property
    def frame(self) -> str:
        """The current animation frame glyph."""
        return FRAMES[self._frame]

    def visible(self) -> Sequence[str]:
        """The rows the preview shows, oldest first.

        Collapsed shows the newest rows so the reader sees what the model is doing
        now. Expansion widens the same window upward through the text already
        received; it never reaches beyond the preview, because the reasoning is not
        kept after the turn and there is no history to scroll.
        """
        if not self._lines:
            return []
        rows = self.rows if not self._expanded else self.rows + self._revealed
        return self._lines[-rows:]

    def expand(self) -> bool:
        """Widen the preview by one step.

        Returns False when the preview is not running: a settled turn has nothing
        left to reveal, so the key must not appear to work.
        """
        if not self.running:
            return False
        self._revealed += self.expanded_rows
        self._expanded = True
        return True

    def header(self) -> str:
        """The preview title: what is happening, and for how long."""
        if self._started is None:
            return ""
        return f"{self.frame} Thinking  {_clock(self.elapsed)}"

    def hint(self) -> str:
        """The key that widens this preview, offered only while it runs.

        A settled turn keeps no reasoning, so there is nothing to reveal and the
        key would do nothing. Advertising it then was the defect this replaces: the
        hint stayed in the scrollback long after it stopped being true.
        """
        return "Ctrl+O: details" if self.running else ""

    def receipt(self) -> str:
        """The one line a settled turn keeps.

        It records that reasoning happened and how long it took, which is the fact
        a reader needs afterwards. The text itself belongs to the trace.
        """
        if self._finished is None or self._started is None:
            return ""
        return f"Thinking  {_clock(self.elapsed)}"

    def clear(self) -> None:
        """Drop the preview entirely, leaving nothing to display."""
        self._lines = []
        self._started = None
        self._finished = None
        self._frame = 0


def _clock(seconds: float) -> str:
    """Seconds as a short duration, matching how a reader estimates a wait."""
    total = int(seconds)
    if total < 60:
        return f"{seconds:.1f}s"
    return f"{total // 60}m{total % 60:02d}s"
