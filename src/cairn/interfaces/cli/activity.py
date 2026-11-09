"""What the turn is doing right now, shown in one place.

Three kinds of transient information used to arrive through three different routes:
reasoning in a preview, tool calls printed straight into the scrollback, and an
interrupt message with nowhere to live. The reader had to look in two places for one
answer to "what is happening", and the scrollback filled with per-call lines that
said nothing once the turn ended.

This module owns that one answer. It holds the current activity, which is one of:

* thinking — the model is reasoning, with a preview and a clock;
* working — one or more tools are running, named;
* interrupting — a stop was requested and cleanup has not finished.

The surface is temporary by construction: it is never printed, so nothing it holds
reaches the transcript. Nothing it holds is added to the transcript afterwards
either — a settled call does not earn a line, because the scrollback is the record of
what was asked and answered, and the trace is the record of how. What a turn keeps
about its tool work is the count the surface already tracked.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class ActivityKind(StrEnum):
    """What the turn is doing, as far as a reader can tell."""

    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    INTERRUPTING = "interrupting"


@dataclass(slots=True)
class Activity:
    """The turn's current activity, and what to say about it.

    Only one activity is current at a time: a turn reasons, then calls a tool, then
    reasons again. Keeping a single value rather than a list is what stops the
    surface from growing into a second transcript.
    """

    _kind: ActivityKind = field(default=ActivityKind.IDLE, repr=False)
    _detail: str = field(default="", repr=False)
    #: Tool calls this turn has finished, which is the one number worth keeping.
    _calls: int = field(default=0, repr=False)
    #: Calls started and not yet finished, by the runtime's call id. A batch runs
    #: several at once, so "is a tool running" is a count rather than a flag: a
    #: single finished call must not report the turn as idle while three others
    #: are still executing.
    _running: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def kind(self) -> ActivityKind:
        return self._kind

    @property
    def detail(self) -> str:
        """What to show for the current activity, naming a batch when there is one."""
        if self._kind is ActivityKind.WORKING:
            return self._working_detail()
        return self._detail

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def running(self) -> int:
        """How many calls are executing right now."""
        return len(self._running)

    @property
    def busy(self) -> bool:
        """Whether anything is happening, so the surface should be shown."""
        return self._kind is not ActivityKind.IDLE

    def thinking(self) -> None:
        """The model is reasoning.

        A tool still running keeps priority: the turn is waiting on the tool, not
        on the model, so reporting "thinking" there would describe work that has
        not started.
        """
        if self._running:
            self._kind = ActivityKind.WORKING
            return
        self._kind = ActivityKind.THINKING
        self._detail = ""

    def started(self, call_id: str | None, label: str) -> None:
        """A tool call started. ``label`` names it the way the caller describes it."""
        if call_id is not None:
            self._running[call_id] = label
        self._kind = ActivityKind.WORKING
        self._detail = label

    def working(self, label: str) -> None:
        """A tool is running without an id to pair it with.

        Kept for a caller that only knows the label; the batch count then cannot be
        tracked, so the surface reports the single label it was given.
        """
        self._kind = ActivityKind.WORKING
        self._detail = label

    def interrupting(self) -> None:
        self._kind = ActivityKind.INTERRUPTING
        self._detail = ""

    def finished_tool(self, call_id: str | None = None) -> None:
        """Count a finished call, and stop claiming to be inside it.

        Returns to thinking only when the batch is settled. A batch that is still
        running keeps the surface on the call that remains, because reporting
        "thinking" while tools execute would say the model resumed when it has not.
        """
        self._calls += 1
        if call_id is not None:
            self._running.pop(call_id, None)
        if self._running:
            self._kind = ActivityKind.WORKING
            self._detail = next(reversed(self._running.values()))
            return
        if self._kind is ActivityKind.WORKING:
            self._kind = ActivityKind.THINKING
            self._detail = ""

    def idle(self) -> None:
        self._kind = ActivityKind.IDLE
        self._detail = ""

    def reset(self) -> None:
        """Forget everything, for the turn that follows."""
        self._kind = ActivityKind.IDLE
        self._detail = ""
        self._calls = 0
        self._running.clear()

    def summary(self) -> str:
        """The one line a finished turn keeps about its tool work.

        A count is enough: the individual calls are in the trace, and listing them
        again is the transcript clutter this replaces. An empty count prints nothing,
        so a turn that called no tool says nothing rather than "0 calls".
        """
        if not self._calls:
            return ""
        noun = "call" if self._calls == 1 else "calls"
        return f"{self._calls} tool {noun}"

    def _working_detail(self) -> str:
        """The call to name while tools run, and how many others share the work."""
        if not self._running:
            return self._detail
        latest = next(reversed(self._running.values()))
        others = len(self._running) - 1
        if others <= 0:
            return latest
        noun = "call" if others == 1 else "calls"
        return f"{latest}  (+{others} concurrent {noun})"
