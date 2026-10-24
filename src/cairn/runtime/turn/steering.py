"""Messages that arrive while a turn is already running.

A turn is a sequence of steps, and a step ends when its tool batch settles. That
boundary is the only place a new instruction can join the conversation without
splitting a tool-call group, so the queue is drained there and nowhere else.

Two decisions shape this module.

**The turn is never restarted.** A message that arrives mid-turn belongs to the
turn that is already open: its identifier, its tool batch and its cancellation
scope all stay. Starting a fresh turn to carry one sentence would discard the work
in progress, which is the opposite of what redirecting means.

**Nothing is dropped.** A message accepted from the operator is either delivered to
the model or handed back when the turn ends, never silently discarded. The
expensive failure here is invisible: a message that vanishes leaves the operator
believing they redirected work that in fact continued unchanged.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

#: How a queued message should be delivered.
#: ``steer`` joins the running turn at its next step boundary; ``queue`` waits for
#: the turn to finish. The names follow what the shipped agents call them, so a
#: reader who knows one of those tools recognises these.
Delivery = Literal["steer", "queue"]


@dataclass(frozen=True, slots=True)
class PendingMessage:
    """One message waiting for a turn boundary."""

    text: str
    delivery: Delivery = "steer"

    @property
    def steers(self) -> bool:
        """Whether this message joins the running turn rather than waiting."""
        return self.delivery == "steer"


@dataclass(slots=True)
class SteerQueue:
    """Messages accepted while a turn runs.

    A message is addressed to a turn, not to the queue: ``steer`` items are consumed
    by the turn in flight, and ``queue`` items survive it. Keeping both in one place
    means the operator sees one list of what they have said, which is what the
    display needs; the split only decides when each is delivered.
    """

    _items: list[PendingMessage] = field(default_factory=list, repr=False)
    #: Set whenever a message arrives, so a turn blocked on a model stream can be
    #: woken to look for it. Cleared by ``drain``.
    _arrived: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def push(self, message: PendingMessage) -> None:
        """Accept a message. Safe to call from any task."""
        if not message.text.strip():
            return
        self._items.append(message)
        self._arrived.set()

    def drain(self) -> list[PendingMessage]:
        """Take every waiting message, oldest first.

        This is the only way a turn learns about new instructions, and it is called
        from one place — the top of the step loop — so two steps can never consume
        the same message.
        """
        if not self._items:
            return []
        taken = self._items
        self._items = []
        self._arrived.clear()
        return taken

    async def wait_for_arrival(self) -> None:
        """Block until a message arrives. Used to preempt a model stream."""
        await self._arrived.wait()

    @property
    def has_pending(self) -> bool:
        return bool(self._items)

    @property
    def has_pending_steers(self) -> bool:
        """Whether a message is waiting to join the turn in flight.

        Checked before a turn finishes: a message accepted at that instant is owed a
        step, and returning would silently drop it.
        """
        return any(item.steers for item in self._items)

    def take_steers(self) -> list[PendingMessage]:
        """Remove and return only the messages addressed to the running turn.

        ``queue`` items stay behind, because they are for after the turn and the
        turn loop is not the component that will deliver them.
        """
        steers = [item for item in self._items if item.steers]
        if not steers:
            return []
        self._items = [item for item in self._items if not item.steers]
        if not self._items:
            self._arrived.clear()
        return steers

    def take_queue(self) -> list[PendingMessage]:
        """Remove and return the messages that outlive this turn."""
        queued = [item for item in self._items if not item.steers]
        if not queued:
            return []
        self._items = [item for item in self._items if item.steers]
        return queued

    def promote(self, index: int) -> bool:
        """Turn a queued message into a steer, addressed to the running turn.

        Returns False when ``index`` does not name a queued message, so the caller
        can leave the selection where it is instead of acting on a stale index.
        """
        queued_positions = [
            position for position, item in enumerate(self._items) if not item.steers
        ]
        if not 0 <= index < len(queued_positions):
            return False
        position = queued_positions[index]
        self._items[position] = PendingMessage(
            text=self._items[position].text, delivery="steer"
        )
        self._arrived.set()
        return True

    def discard(self, index: int) -> bool:
        """Remove one queued message by position among the queued messages."""
        queued_positions = [
            position for position, item in enumerate(self._items) if not item.steers
        ]
        if not 0 <= index < len(queued_positions):
            return False
        del self._items[queued_positions[index]]
        if not self._items:
            self._arrived.clear()
        return True

    def queued(self) -> Sequence[PendingMessage]:
        """The messages that will wait for the turn to finish."""
        return tuple(item for item in self._items if not item.steers)

    def __len__(self) -> int:
        return len(self._items)
