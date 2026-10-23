"""The seam between a streaming display and wherever it paints.

A streaming display needs three things from its output surface: start drawing,
replace what is drawn, stop. Nothing else. Stating that as a protocol keeps the
display independent of *how* the region is painted, which matters because the two
available hosts cannot coexist:

* ``rich.Live`` paints a transient region by moving the cursor itself, and it
  cannot share a terminal with a ``prompt_toolkit`` application — the application
  repaints over it and invalidates its diff cache;
* the frame's live tail paints inside an application that owns the bottom of the
  terminal, so that output stays selectable and the input keeps its position.

The display therefore depends on this protocol, and the CLI decides which host to
construct. Keeping the contract here rather than inside either implementation is
what lets the same streaming logic serve both without knowing either.
"""

from typing import Protocol, runtime_checkable

from rich.console import RenderableType


@runtime_checkable
class LiveRegion(Protocol):
    """A region that can be drawn, redrawn in place, and taken down.

    Implementations own their own pacing: ``update`` replaces what is shown, and
    the host decides when a repaint is actually worth doing. A caller must not
    assume that ``update`` paints immediately.
    """

    def start(self, renderable: RenderableType) -> None:
        """Begin drawing, with the first frame's content."""
        ...

    def update(self, renderable: RenderableType) -> None:
        """Replace the drawn content. Repeated calls may be coalesced."""
        ...

    def stop(self) -> None:
        """Take the region down, leaving whatever the host considers settled."""
        ...
