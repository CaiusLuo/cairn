"""The action surface: a decision the operator makes now, above the composer.

A command used to answer by printing. `/model` printed the group and its IDs, an
unknown command printed a correction, and a provider switch printed its endpoint —
all of it into the transcript, where it stayed for the rest of the session. The
transcript is the record of what was asked and answered; a correction about a typo,
or a list of commands, is none of those things.

This module owns the shape of that answer instead. A command produces an
``Action``: a short message, optionally with a set of choices, and the caller
decides where it is shown. In a terminal it is the bottom row above the composer,
where the operator is already looking; in a script it is plain text, because a
pipeline reads lines and cannot press a key.

The surface is temporary by construction. ``Action`` holds no history: the next
message replaces the last, and pressing Escape or editing the composer clears it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Tone(StrEnum):
    """How much emphasis an action deserves.

    A correction and a refusal are the two the operator must not miss, so they carry
    the higher emphasis the design reserves for them; everything else is dim, because
    a menu is not news.
    """

    INFO = "info"
    CORRECTION = "correction"
    REFUSAL = "refusal"


@dataclass(frozen=True, slots=True)
class ActionRow:
    """One selectable row of an action, as it is displayed."""

    name: str
    description: str = ""
    #: Whether this row is the current value, marked so a chooser shows what is
    #: already chosen without the operator having to remember.
    current: bool = False
    #: Whether choosing this row declines. A decision that grants something — trust,
    #: a permission — must be able to say which row is the safe one, because the
    #: cursor starts there and Enter on an untouched menu must not grant anything.
    cancel: bool = False


@dataclass(slots=True)
class Action:
    """Something to show the operator, and possibly to choose between.

    ``message`` is the line or two that states the fact; ``rows`` turns the same
    surface into a chooser. A message with no rows is a correction or a status note:
    the operator reads it and edits the composer. A message with rows is a decision,
    and the keys ``Up``/``Down``/``Enter``/``Esc`` apply to it.
    """

    message: str
    rows: Sequence[ActionRow] = ()
    tone: Tone = Tone.INFO
    #: The keys this surface answers, shown only while it is open. A hint that is
    #: always on screen stops being read.
    hint: str = ""
    #: The row the operator has chosen, by index into ``rows``.
    selected: int = field(default=0)
    #: Whether this action is asking for authority rather than for a preference.
    #: A question of authority starts on its Cancel row and is not answered by
    #: editing the composer, so the surface must not treat its dismissal as consent.
    decides_authority: bool = False

    @property
    def choosable(self) -> bool:
        """Whether the operator has something to move between."""
        return bool(self.rows)

    def selected_row(self) -> ActionRow | None:
        """The row under the cursor, or ``None`` when there is nothing to choose."""
        if not self.rows:
            return None
        return self.rows[min(max(0, self.selected), len(self.rows) - 1)]

    def clamped(self) -> "Action":
        """This action with its selection forced into range.

        Rows are supplied by the caller and can shrink between messages; a stale
        index would otherwise point past the list and select nothing.
        """
        if not self.rows:
            self.selected = 0
            return self
        self.selected = max(0, min(len(self.rows) - 1, self.selected))
        return self


def correction(message: str) -> Action:
    """A short statement about what the operator typed, cleared by the next edit."""
    return Action(message=message, tone=Tone.CORRECTION)


def refusal(message: str) -> Action:
    """A decision that did not go through, with the reason it did not."""
    return Action(message=message, tone=Tone.REFUSAL)


def note(message: str) -> Action:
    """A quiet fact worth stating once, such as what a switch left the session on."""
    return Action(message=message, tone=Tone.INFO)


def consent(message: str, *, grant: str, decline: str = "Cancel") -> Action:
    """Ask for authority, with the cursor on declining.

    ``grant`` and ``decline`` are the two rows; the selection starts on ``decline``
    so that Enter or a stray keystroke cannot grant anything the operator has not
    read. The message carries the whole decision, because a reader who cannot see
    where the conversation is about to be sent cannot consent to sending it.
    """
    return Action(
        message=message,
        rows=(
            ActionRow(name=grant),
            ActionRow(name=decline, cancel=True),
        ),
        selected=1,
        hint="Up/Down move   Enter confirm   Esc cancel",
        tone=Tone.REFUSAL,
        decides_authority=True,
    )
