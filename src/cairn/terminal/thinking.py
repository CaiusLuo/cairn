"""The reasoning preview, which lives only while a turn runs.

Reasoning is worth watching while it happens and worth nothing afterwards: a reader
who wants the argument wants the answer, and a reader who wants the evidence wants
the trace. So the preview is temporary by design, and the only thing it leaves
behind is a receipt saying how long it took.

That is the difference this module exists to enforce. The earlier behaviour
reprinted the whole block into the scrollback when the turn settled, which cost a
screenful of text per turn and left a `Ctrl+O` hint that could no longer do
anything. Both are consequences of treating temporary text as permanent.

The window is measured in *rendered rows*, not in text lines or in provider deltas.
A delta is an artefact of how a provider chops its stream: it can end mid-sentence,
mid-word, or mid-character, and a preview that broke a row wherever a delta ended
would show four fragments of one sentence and call them two lines. The window is
therefore the tail of the text as the terminal would wrap it at the current width,
recomputed whenever the width changes.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from prompt_toolkit.utils import get_cwidth

#: Rendered rows kept in the preview. Two is enough to show what the model is
#: doing and small enough that the answer keeps the screen.
PREVIEW_ROWS = 2

#: Rows one expansion adds. A whole step keeps the reveal predictable rather than
#: moving the window one row at a time.
EXPANDED_ROWS = PREVIEW_ROWS * 3

#: The animation frames, all one character wide so the gutter never shifts. A
#: multi-cell glyph would misalign on a terminal that renders it wide. All four are
#: ASCII because a spinner is the last place to spend a font dependency.
FRAMES = (".", "*", "+", "*")

#: Seconds between frames. Four per second is legible and cheap; anything faster
#: reads as flicker and costs a repaint over a slow link for no added signal.
FRAME_INTERVAL_SECONDS = 0.25

#: Columns assumed before anything has measured the terminal. Wide enough that the
#: preview is not needlessly narrow, and replaced as soon as the real width is known.
DEFAULT_WIDTH = 80


@dataclass(slots=True)
class ThinkingPreview:
    """The two-row window onto reasoning that is still arriving.

    The window follows the *newest* rows. Showing the opening rows was the earlier
    behaviour and it aged badly: by the time a reader looked, the preview had been
    describing the start of a thought the model had already moved past.
    """

    rows: int = PREVIEW_ROWS
    expanded_rows: int = EXPANDED_ROWS
    #: The reasoning as one growing string. Kept whole, not per delta, so wrapping
    #: is decided by the text and the width rather than by where a chunk ended.
    _text: str = field(default="", repr=False)
    _started: float | None = field(default=None, repr=False)
    _finished: float | None = field(default=None, repr=False)
    _elapsed: float = field(default=0.0, repr=False)
    _interval_started: float | None = field(default=None, repr=False)
    #: The clock's last reading, supplied by whoever trusts the wall clock most:
    #: the animation tick while running, the settle moment when it ends.
    _now: float | None = field(default=None, repr=False)
    _frame: int = field(default=0, repr=False)
    #: When the current frame was drawn, so a new one is due on the interval rather
    #: than on every tick.
    _frame_at: float = field(default=0.0, repr=False)
    #: Rows the operator asked for on top of the collapsed window. Zero collapsed.
    _extra_rows: int = field(default=0, repr=False)
    #: Columns the preview wraps at, recomputed on resize.
    width: int = DEFAULT_WIDTH

    def __post_init__(self) -> None:
        if self.rows < 1:
            raise ValueError("rows must be at least one")

    # -- lifecycle -------------------------------------------------------

    def begin(self, *, at: float) -> None:
        """Start a new preview, discarding anything left from the last one."""
        self._text = ""
        self._started = at
        self._now = at
        self._finished = None
        self._elapsed = 0.0
        self._interval_started = at
        self._frame = 0
        self._frame_at = at
        self._extra_rows = 0

    def append(self, text: str) -> None:
        """Add newly arrived reasoning.

        The chunk boundary is not a line break: deltas are concatenated and the
        wrapping happens afterwards. Breaking on each delta was what turned one
        sentence into four rows of fragments.
        """
        if text:
            self._text += text

    def tick(self, *, at: float) -> bool:
        """Advance the clock and the spinner; say whether the frame changed.

        This is what makes the clock and the spinner run while the model is silent.
        Without it they only moved when a delta arrived, so a slow first token
        showed a frozen `00:00` and a still glyph — indistinguishable from a hang.
        The caller drives this from its repaint timer, which is not another Agent
        loop: it only decides what the current frame looks like.
        """
        if not self.running:
            return False
        self._now = at
        if at - self._frame_at < FRAME_INTERVAL_SECONDS:
            return False
        self._frame_at = at
        self._frame = (self._frame + 1) % len(FRAMES)
        return True

    def finish(self, *, at: float) -> None:
        """Stop the preview. Its text is kept only until the receipt is written."""
        if self._finished is None and self._interval_started is not None:
            self._elapsed += max(0.0, at - self._interval_started)
            self._interval_started = None
        self._finished = at
        self._now = at

    def resume(self, *, at: float) -> None:
        """Resume timing after a tool or other non-reasoning interval."""
        if self._started is None or self._finished is None:
            return
        self._finished = None
        self._now = at
        self._interval_started = at
        self._frame_at = at

    def clear(self) -> None:
        """Drop the preview entirely, leaving nothing to display."""
        self._text = ""
        self._started = None
        self._now = None
        self._finished = None
        self._elapsed = 0.0
        self._interval_started = None
        self._frame = 0
        self._frame_at = 0.0
        self._extra_rows = 0

    # -- state -----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the preview is still being written.

        Only a running preview animates and offers its key hint; a finished one has
        nothing left to reveal, so advertising the key would be a lie.
        """
        return self._started is not None and self._finished is None

    @property
    def elapsed(self) -> float:
        """Seconds the reasoning took, or has been running.

        While running this is the clock's last reading rather than the start time.
        Returning the start made the header show `00:00` forever, which is what a
        clock that does not run looks like.
        """
        if self._started is None:
            return 0.0
        end = self._finished if self._finished is not None else self._now
        if end is None:
            end = self._started
        active = (
            max(0.0, end - self._interval_started)
            if self._finished is None and self._interval_started is not None
            else 0.0
        )
        return self._elapsed + active

    def has_content(self) -> bool:
        """Whether any reasoning arrived, so a receipt is worth printing.

        A turn that produced no reasoning gets no receipt: an empty "Thinking 0.0s"
        would claim a step that never happened.
        """
        return bool(self._text.strip())

    def set_width(self, width: int | None) -> bool:
        """Adopt the terminal width the preview wraps at; say whether it changed.

        A resize changes which rows exist, so the tail is recomputed rather than
        kept from the old width.
        """
        if width is None or width < 1 or width == self.width:
            return False
        self.width = width
        return True

    # -- display ---------------------------------------------------------

    @property
    def frame(self) -> str:
        """The current animation frame glyph."""
        return FRAMES[self._frame]

    def advance(self) -> None:
        """Move to the next animation frame."""
        self._frame = (self._frame + 1) % len(FRAMES)

    def expanded(self) -> bool:
        """Whether the operator has widened the window past the collapsed size."""
        return self._extra_rows > 0

    def rows_shown(self) -> int:
        """How many rendered rows the window currently asks for."""
        return self.rows + self._extra_rows

    def rendered_rows(self, *, width: int | None = None) -> Sequence[str]:
        """The whole reasoning as the rows a terminal would draw at ``width``.

        Wrapping is done here rather than left to the renderer because the window
        slices rows, and a slice of logical lines would cut a sentence in a place
        the terminal never would.
        """
        return _wrap_text(self._text, self._columns(width))

    def visible(self, *, width: int | None = None) -> Sequence[str]:
        """The rows the preview shows, oldest first.

        Collapsed shows the newest rows so the reader sees what the model is doing
        now. Expansion widens the same window upward through the text already
        received; it never reaches beyond the preview, because the reasoning is not
        kept after the turn and there is no history to scroll.
        """
        rows = self.rendered_rows(width=width)
        if not rows:
            return []
        return rows[-self.rows_shown() :]

    def toggle_expanded(self) -> bool:
        """Widen the preview, or put it back to the collapsed window.

        Returns False when the preview is not running: a settled turn has nothing
        left to reveal, so the key must not appear to work. Toggling rather than
        only growing is what makes the key's promise true — the header offers one
        key, so that key has to be able to undo itself.
        """
        if not self.running:
            return False
        self._extra_rows = 0 if self._extra_rows else self.expanded_rows
        return True

    def header(self) -> str:
        """The preview title: what is happening, and for how long.

        One frame character, then the label and the clock. The glyph is the only
        thing that moves, so the header's shape stays stable while it animates.
        """
        if self._started is None:
            return ""
        return f"{self.frame} Thinking  {_clock(self.elapsed)}"

    def hint(self) -> str:
        """The key that widens this preview, offered only while it runs.

        A settled turn keeps no reasoning, so there is nothing to reveal and the
        key would do nothing. Advertising it then was the defect this replaces: the
        hint stayed in the scrollback long after it stopped being true.
        """
        if not self.running:
            return ""
        return "Ctrl+O: less" if self.expanded() else "Ctrl+O: details"

    def receipt(self) -> str:
        """The one line a settled turn keeps.

        It records that reasoning happened and how long it took, which is the fact
        a reader needs afterwards. The text itself belongs to the trace.
        """
        if self._finished is None or self._started is None:
            return ""
        return f"Thinking  {_clock(self.elapsed)}"

    # -- internals -------------------------------------------------------

    def _columns(self, width: int | None) -> int:
        columns = width if width is not None and width > 0 else self.width
        return max(1, columns)


def _wrap_text(text: str, width: int) -> list[str]:
    """Text as the rows a terminal draws at ``width`` columns.

    Rows are cut by *display cells*, not by characters. A CJK glyph occupies two
    cells, a combining mark occupies none, and an emoji usually two, so slicing by
    index would put more than ``width`` cells on a row for one audience and fewer for
    another — and the window would then show a different number of lines than the
    terminal does. ``get_cwidth`` is prompt_toolkit's own table, already a dependency here to
    decide the same question.

    Each logical line is wrapped independently so an explicit newline still starts a
    row, and rows are filled rather than broken at word boundaries: a terminal fills
    a row and continues on the next one. Blank rows are dropped, because a blank line
    in the reasoning is not a row the preview should spend on nothing.
    """
    rows: list[str] = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        rows.extend(_split_cells(stripped, width))
    return rows


def _split_cells(line: str, width: int) -> list[str]:
    """One logical line as rows of at most ``width`` display cells.

    A glyph wider than the whole row is emitted on its own rather than dropped: a
    two-cell glyph in a one-cell window is unrenderable, and losing it would hide
    text the model actually produced.
    """
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for char in line:
        cells = get_cwidth(char)
        if cells < 0:
            # A zero-width or unprintable character carries no cell of its own; it
            # joins whatever is already there so a combining mark stays on its base.
            current.append(char)
            continue
        if used + cells > width and current:
            rows.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += cells
    if current:
        rows.append("".join(current))
    return rows


def _clock(seconds: float) -> str:
    """Seconds as a short duration, matching how a reader estimates a wait."""
    total = int(seconds)
    if total < 60:
        return f"{seconds:.1f}s"
    return f"{total // 60}m{total % 60:02d}s"
