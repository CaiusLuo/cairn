"""Interrupting a running turn, and what it leaves behind.

An interrupt is a state, not a keystroke. The operator presses a key, and for a
moment the turn is neither running nor finished: the cancellation has been requested
and the cleanup has not completed. Showing nothing during that gap reads as a hang,
so the surface says what is happening.

What an interrupted turn leaves is a receipt, not an apology. A partial answer is
discarded — a half-written table reads as corruption once it stops changing — but the
fact that a turn was interrupted, and how long it ran, is worth keeping. The reader
who interrupted it knows what they did; a later reader does not.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class Interruption:
    """Whether a turn is being interrupted, and what it left.

    The two flags are separate because they answer different questions. ``pending``
    drives the transient message while cleanup runs; ``settled`` guards against
    reporting the same interruption twice, since a cancelled turn can raise more
    than once on its way out.
    """

    _pending: bool = field(default=False, repr=False)
    _settled: bool = field(default=False, repr=False)
    _started: float | None = field(default=None, repr=False)

    def begin(self, *, at: float) -> bool:
        """Mark an interrupt as requested.

        Returns False when one is already pending, so the caller does not re-request
        a cancellation that is already in flight.
        """
        if self._pending or self._settled:
            return False
        self._pending = True
        self._started = at
        return True

    def settle(self) -> bool:
        """Mark the interruption as finished.

        Returns False when nothing was pending, so a turn that ended normally does
        not leave an interrupt receipt behind.
        """
        if not self._pending:
            return False
        self._pending = False
        self._settled = True
        return True

    def reset(self) -> None:
        """Forget the last interruption, for the turn that follows it."""
        self._pending = False
        self._settled = False
        self._started = None

    @property
    def pending(self) -> bool:
        """Whether a cancellation is in flight, so the surface can say so."""
        return self._pending

    @property
    def settled(self) -> bool:
        return self._settled

    def message(self) -> str:
        """What the surface says while the interrupt is in flight."""
        return "Interrupting…" if self._pending else ""

    def receipt(self) -> str:
        """What the transcript keeps once the interrupt completes.

        It names the outcome and the elapsed time. Saying only "interrupted" leaves
        the reader unsure whether the turn had done anything before it stopped.
        """
        if not self._settled:
            return ""
        return "Interrupted"
