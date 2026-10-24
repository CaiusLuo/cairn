"""Handing a submitted line from the input line to the turn loop.

The input line runs for the whole interactive session, so a submitted line is not
returned from a function call — it is delivered to whoever is waiting for the next
message. This is that handoff, kept as its own object so the reason is visible: the
turn and the input are not nested, they overlap, and something has to carry the line
between them.

A closed queue releases a waiting reader with ``EOFError`` rather than blocking for
ever, because the session ending is exactly the case where nobody will ever submit
another line.
"""

import asyncio
from dataclasses import dataclass, field


@dataclass(slots=True)
class LineQueue:
    """Submitted lines waiting to become turns.

    Blank input is dropped at submission: it is a keystroke, not a message, and
    treating it as a turn would start work on nothing.
    """

    _pending: asyncio.Queue[str] = field(default_factory=asyncio.Queue, repr=False)
    _closed: bool = field(default=False, repr=False)
    #: Readers suspended in ``next_line`` when the session closed. Woken explicitly
    #: because a closed queue will never produce a line for them to receive.
    _waiters: list[asyncio.Future[str]] = field(default_factory=list, repr=False)

    def submit(self, text: str) -> None:
        """Offer a submitted line. A blank line is ignored, not delivered."""
        if self._closed:
            raise EOFError("the session is closed")
        if not text or not text.strip():
            return
        self._pending.put_nowait(text)
        self._release_one_waiter()

    async def next_line(self) -> str:
        """Wait for the next submitted line.

        Raises:
            EOFError: the session closed, so no line will arrive.
        """
        if not self._pending.empty():
            return self._pending.get_nowait()
        if self._closed:
            raise EOFError("the session is closed")
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            return await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _release_one_waiter(self) -> None:
        """Hand a queued line straight to a suspended reader, if there is one."""
        if not self._waiters or self._pending.empty():
            return
        waiter = self._waiters.pop(0)
        if not waiter.done():
            waiter.set_result(self._pending.get_nowait())

    def close(self) -> None:
        """Stop accepting lines and release anyone waiting for one."""
        self._closed = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(EOFError("the session is closed"))
        self._waiters.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return self._pending.qsize()
