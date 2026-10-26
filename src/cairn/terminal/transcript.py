"""The transcript a full-screen CLI owns, and the window onto it.

A full-screen layout cannot rely on the terminal's own scrollback: the screen
belongs to the application, so the output history has to be kept and scrolled by
us. This module owns that history and answers the one question the renderer asks
on every paint — which lines fit.

It is deliberately free of prompt_toolkit and rich. Keeping the window arithmetic
here means it can be tested exactly, including the cases that are miserable to
reproduce by hand: a resize, an empty transcript, and a transcript longer than the
pane.
"""

from dataclasses import dataclass, field

#: The one intentional blank row between the live surface and the composer.
TRANSCRIPT_GAP_LINES = 1


@dataclass(slots=True)
class Transcript:
    """Append-only output history with a scroll position.

    ``follow`` means the view is pinned to the newest line, which is what a reader
    wants while work streams. Scrolling up turns it off, so new output cannot yank
    the view away from what the reader is looking at; returning to the bottom
    turns it back on.
    """

    _lines: list[str] = field(default_factory=list)
    _offset_from_bottom: int = 0

    def append(self, text: str) -> None:
        """Add ``text`` as lines, keeping the view pinned when it already was.

        A scrolled-up view keeps the lines it is showing: new output must not
        shift what a reader is looking at, so the offset grows by exactly as many
        lines as arrived.
        """
        if not text:
            return
        new_lines = text.split("\n")
        # A trailing newline means the last line is complete; drop the empty
        # remainder rather than growing a blank line per print.
        if new_lines and new_lines[-1] == "":
            new_lines.pop()
        if not new_lines:
            return
        if not self.follow:
            self._offset_from_bottom += len(new_lines)
        self._lines.extend(new_lines)

    @property
    def follow(self) -> bool:
        return self._offset_from_bottom == 0

    @property
    def line_count(self) -> int:
        return len(self._lines)

    def window(self, height: int) -> list[str]:
        """The lines visible at the current scroll position.

        The view is a pane of ``height`` rows whose bottom sits ``offset`` rows
        above the newest line. While the transcript is shorter than the pane, or
        while the view is scrolled to the very top, the pane simply runs out of
        content to show; it never collapses to nothing just because the offset
        reached the end of the history.
        """
        if height <= 0 or not self._lines:
            return []
        total = len(self._lines)
        # The view may sit at most so far up that its top row is the first line.
        offset = min(self._offset_from_bottom, max(0, total - height))
        end = total - offset
        start = max(0, end - height)
        return self._lines[start:end]

    def scroll_up(self, lines: int = 1) -> None:
        """Move the view towards older output, stopping at the first line."""
        if lines <= 0:
            return
        self._offset_from_bottom = min(
            len(self._lines), self._offset_from_bottom + lines
        )

    def scroll_down(self, lines: int = 1) -> None:
        """Move the view towards newer output; reaching the end resumes following."""
        if lines <= 0:
            return
        self._offset_from_bottom = max(0, self._offset_from_bottom - lines)

    def scroll_to_bottom(self) -> None:
        self._offset_from_bottom = 0
