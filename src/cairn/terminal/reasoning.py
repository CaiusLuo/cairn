"""How much of the model's reasoning a reader is shown.

Reasoning is useful and voluminous: worth keeping, not worth reading by default.
This module owns that judgement — whether the block is collapsed, how many lines a
collapsed block shows, and what the reader has to do to see the rest — so the
display code never decides it inline.

The choice of keyboard over mouse is deliberate. Clicking would require capturing
the terminal's mouse, and that costs the native drag-select and copy that the
bottom-viewport layout exists to preserve; a terminal that eats the mouse is worse
than one that needs a keystroke. It also matches what the shipped agents do, for
the same reason.
"""

from dataclasses import dataclass, field

#: Lines shown while collapsed. Enough to show that thinking happened and what it
#: was about, short enough that it never displaces the answer being written.
COLLAPSED_REASONING_LINES = 2

#: Lines shown per expansion. A long chain of thought is read in pages rather than
#: dumped whole, so expanding once cannot bury the answer under forty lines.
EXPANDED_REASONING_STEP = 20


@dataclass(slots=True)
class ReasoningView:
    """How much of the reasoning is visible, and how to reveal more.

    The full text is retained regardless: collapsing changes what is shown, never
    what was received. Expanding is a chain of bounded steps rather than a single
    all-or-nothing toggle, so a very long block stays navigable.
    """

    collapsed_lines: int = COLLAPSED_REASONING_LINES
    expanded_step: int = EXPANDED_REASONING_STEP
    _revealed: int = field(default=0, repr=False)
    _expanded: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.collapsed_lines < 0:
            raise ValueError("collapsed_lines must not be negative")
        if self.expanded_step < 1:
            raise ValueError("expanded_step must be at least one line")

    @property
    def expanded(self) -> bool:
        """Whether the reader has asked to see more than the collapsed summary."""
        return self._expanded

    def visible_lines(self, total: int) -> int:
        """How many of ``total`` available lines this view shows.

        Collapsed shows a fixed summary; expanded grows by whole steps so every
        reveal is a predictable amount rather than one line at a time.
        """
        if total <= 0:
            return 0
        if not self._expanded:
            return min(total, self.collapsed_lines)
        shown = self.collapsed_lines + max(1, self._revealed) * self.expanded_step
        return min(total, shown)

    def hidden_lines(self, total: int) -> int:
        """Lines the reader has not been shown yet."""
        return max(0, total - self.visible_lines(total))

    def expand(self) -> bool:
        """Show the next step. Returns False when nothing more could be revealed."""
        self._expanded = True
        self._revealed += 1
        return True

    def collapse(self) -> None:
        """Return to the summary, forgetting earlier expansion."""
        self._expanded = False
        self._revealed = 0

    def note(self, hidden: int) -> str:
        """The hint shown when lines are withheld.

        It names the key and the remaining count, because a silent truncation reads
        as lost content rather than as collapsed content.
        """
        if hidden <= 0:
            return ""
        return f"… {hidden} more lines · Ctrl+O to expand"
