"""The command palette: what `/` offers, filtered as it is typed.

The palette is a temporary surface, not transcript text. It opens above the
composer while the operator is choosing and leaves nothing behind when it closes,
because a list of commands is a means to an action rather than a record of one.

Filtering is by prefix, not substring. The operator types `/mo` meaning "the
command that starts with mo", and a palette that answered with `/compact` because
it contains an `m` would be guessing.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

#: What the palette accepts as a command name, with its one-line purpose. The
#: catalogue lives here rather than in the renderer so the surface and the `/help`
#: text cannot drift apart.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/model", "Choose the active model group"),
    ("/provider", "Switch the conversation provider"),
    ("/compact", "Reduce conversation context"),
    ("/trace", "Inspect stored execution details"),
    ("/help", "Show command help"),
    ("/exit", "Exit Cairn"),
)

#: Rows the palette shows before it scrolls. Small enough that the composer keeps
#: its place; the list is short enough that scrolling is rare.
MAX_ROWS = 6


@dataclass(frozen=True, slots=True)
class CommandRow:
    """One palette entry as it is displayed."""

    name: str
    description: str
    selected: bool


@dataclass(slots=True)
class Palette:
    """The open palette: what is typed, which row is chosen, and what to show.

    Closed is the default state: the surface must not appear until the operator
    asks for it, and it must disappear the moment they do not need it.
    """

    _typed: str = field(default="", repr=False)
    _selected: int = field(default=0, repr=False)
    _open: bool = field(default=False, repr=False)

    @property
    def open(self) -> bool:
        return self._open

    def update(self, typed: str) -> None:
        """Track the composer's text, opening for a leading slash and closing otherwise.

        The palette follows the text rather than a key, so it appears the moment the
        operator starts a command and vanishes if they change their mind — including
        when they delete the slash.
        """
        if not typed.startswith("/"):
            self.close()
            return
        was_open = self._open
        self._open = True
        self._typed = typed
        if not was_open:
            # A fresh command starts at the top; a continuing one keeps its place.
            self._selected = 0
        self._clamp()

    def close(self) -> None:
        """Hide the palette and forget what was chosen."""
        self._open = False
        self._typed = ""
        self._selected = 0

    def matches(self) -> Sequence[tuple[str, str]]:
        """The commands matching what has been typed, in catalogue order.

        A closed palette matches nothing: it is not showing anything, so it cannot
        have a chosen command either.
        """
        if not self._open:
            return ()
        prefix = self._typed.strip().lower().split()[0] if self._typed.strip() else ""
        if not prefix:
            return COMMANDS
        return tuple(item for item in COMMANDS if item[0].startswith(prefix))

    def rows(self) -> list[CommandRow]:
        """The visible rows, with the chosen one marked."""
        matches = self.matches()[:MAX_ROWS]
        return [
            CommandRow(
                name=name, description=description, selected=index == self._selected
            )
            for index, (name, description) in enumerate(matches)
        ]

    def move(self, step: int) -> None:
        """Move the selection by ``step`` rows, stopping at either end."""
        matches = self.matches()
        if not matches:
            return
        self._selected = max(0, min(len(matches[:MAX_ROWS]) - 1, self._selected + step))

    def selected_command(self) -> str:
        """The command the operator has chosen, or an empty string when none matches."""
        matches = self.matches()
        if not matches:
            return ""
        return matches[min(self._selected, len(matches) - 1)][0]

    def _clamp(self) -> None:
        limit = min(len(self.matches()), MAX_ROWS)
        self._selected = max(0, min(limit - 1, self._selected)) if limit else 0

    def hint(self) -> str:
        """The keys the palette answers, shown only while it is open."""
        if not self._open:
            return ""
        return "Up/Down move · Tab or Enter complete · Esc close"
