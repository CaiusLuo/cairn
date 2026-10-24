"""What the operator sees about messages they have already typed.

While a turn runs, a typed message either joins it (steer) or waits for it (queue).
Both need to be visible: a message accepted but never shown is indistinguishable
from one that was dropped, and the operator would type it again.

Queued messages are shown as a stack above the input, oldest first, with the
selected one marked. Selection exists because a message queued a moment ago may
become urgent: the operator can point at it and ask for it to steer instead of
waiting.
"""

from dataclasses import dataclass, field

from cairn.runtime.turn.steering import SteerQueue


@dataclass(frozen=True, slots=True)
class QueueRow:
    """One queued message as it is displayed."""

    index: int
    text: str
    selected: bool


@dataclass(slots=True)
class QueueView:
    """The queued messages, and which one the operator has selected.

    Selection is kept by index into the queued messages, not into the queue: steers
    are consumed by the running turn and would otherwise shift the selection onto a
    different message.
    """

    _selected: int = field(default=0, repr=False)

    def rows(self, queue: SteerQueue) -> list[QueueRow]:
        """The messages waiting for the turn, with the selected one marked."""
        queued = list(queue.queued())
        if not queued:
            self._selected = 0
            return []
        self._selected = min(self._selected, len(queued) - 1)
        return [
            QueueRow(
                index=position,
                text=item.text,
                selected=position == self._selected,
            )
            for position, item in enumerate(queued)
        ]

    def move_up(self, queue: SteerQueue) -> None:
        """Select the message above, stopping at the oldest."""
        if not queue.queued():
            return
        self._selected = max(0, self._selected - 1)

    def move_down(self, queue: SteerQueue) -> None:
        """Select the message below, stopping at the newest."""
        queued = queue.queued()
        if not queued:
            return
        self._selected = min(len(queued) - 1, self._selected + 1)

    @property
    def selected(self) -> int:
        return self._selected

    def hint(self, *, running: bool) -> str:
        """The key hint shown under the composer.

        The hint names the current meaning of the two keys only while a turn is
        running, because outside a turn both keys simply submit.
        """
        if not running:
            return ""
        return "Enter = guide the running turn · Tab = queue for after it"

    def promote_selected(self, queue: SteerQueue) -> bool:
        """Turn the selected message into a steer, and move the selection back.

        Returns False when there is nothing queued, so the caller can leave the
        turn alone instead of acting on a stale selection.
        """
        if not queue.queued():
            return False
        if not queue.promote(self._selected):
            return False
        self._selected = max(0, self._selected - 1)
        return True
