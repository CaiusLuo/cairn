"""The command palette: a temporary, navigable surface above the composer.

The palette is not transcript text. It opens while the operator is choosing and
leaves nothing behind when it closes, because a list of commands is a means to an
action rather than a record of one.

It is a stack of levels rather than a flat list. ``/model`` does not run a command
that then prints something into the conversation; it descends into a level listing
what that command accepts, and ``Left`` comes back. The keys mean the same thing at
every level, which is what makes the surface learnable:

* ``Up``/``Down`` move within a level,
* ``Right`` opens the chosen entry's next level; ``Tab`` completes the command,
* ``Left`` goes up one level,
* ``Esc`` closes the whole palette, leaving the transcript untouched,
* ``Enter`` runs what is chosen: it opens a level while the choice has one, and
  submits the command once the choice is complete.
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

#: The next level a descending command offers: what that command accepts. These are
#: sub-commands, not session data — the palette stays a pure surface, and the caller
#: supplies whatever a chosen level needs to act on.
CHILDREN: dict[str, tuple[tuple[str, str], ...]] = {
    "/model": (
        ("use", "Select a model group for this session"),
        ("list", "Show groups with their primary and fallback IDs"),
        ("add", "Append a model ID to a group"),
        ("remove", "Delete a model ID from a group"),
        ("move", "Reorder a model ID within its group"),
    ),
    "/provider": (
        ("use", "Switch the conversation provider"),
        ("list", "Show configured providers"),
    ),
}

#: Commands that open a further level rather than acting immediately. A command not
#: listed here is submitted to the runtime unchanged.
DESCENDS = frozenset(CHILDREN)

#: Rows shown before a level scrolls. Small enough that the composer keeps its place;
#: the lists are short enough that scrolling is rare.
MAX_ROWS = 6


@dataclass(frozen=True, slots=True)
class CommandRow:
    """One palette entry as it is displayed."""

    name: str
    description: str
    selected: bool


@dataclass(slots=True)
class Palette:
    """The open palette: which level, which row, and what has been typed.

    Each level remembers its own selection, so returning to a level puts the cursor
    back where it was rather than at the top.
    """

    _typed: str = field(default="", repr=False)
    _open: bool = field(default=False, repr=False)
    #: One entry per level: that level's rows and its selected index.
    _stack: list[tuple[tuple[tuple[str, str], ...], int]] = field(
        default_factory=list, repr=False
    )
    #: The names chosen on the way down, one per level below the root.
    _trail: list[str] = field(default_factory=list, repr=False)

    # -- state -----------------------------------------------------------

    @property
    def open(self) -> bool:
        return self._open

    @property
    def depth(self) -> int:
        """How many levels are open; one is the command list."""
        return len(self._stack)

    def update(self, typed: str) -> None:
        """Track the composer's text, opening for a leading slash and closing otherwise.

        The palette follows the text rather than a key, so it appears the moment a
        command starts and vanishes if the operator changes their mind — including
        when the slash is deleted. Typing returns to the first level, because the
        filter applies to the command list and not to a submenu.
        """
        if not typed.startswith("/"):
            self.close()
            return
        self._typed = typed
        self._open = True
        self._stack = [(self._matching(), 0)]
        self._trail = []

    def close(self) -> None:
        """Hide the palette and forget where the operator had navigated."""
        self._open = False
        self._typed = ""
        self._stack = []
        self._trail = []

    # -- navigation ------------------------------------------------------

    def move(self, step: int) -> None:
        """Move the selection within the current level, stopping at either end."""
        rows, selected = self._current()
        if not rows:
            return
        limit = min(len(rows), MAX_ROWS) - 1
        self._stack[-1] = (rows, max(0, min(limit, selected + step)))

    def settles(self) -> bool:
        """Whether the chosen entry is complete as it stands.

        A command with no next level is a whole command line: ``/exit`` is finished
        the moment it is chosen, and nothing is left to type. A command that opens a
        level is not, because the level is what chooses the rest of the line.
        """
        return self.selected_name() not in DESCENDS

    def descend(self) -> bool:
        """Open the selected entry's next level.

        Returns False when the selection leads nowhere, so the caller can act on the
        choice instead. A row that already ran its action is not re-run.
        """
        command = self.selected_name()
        if command not in DESCENDS:
            return False
        children = CHILDREN[command]
        self._trail.append(command)
        self._stack.append((children, 0))
        return True

    def ascend(self) -> bool:
        """Go back one level. Returns False at the command list, which is the root."""
        if len(self._stack) <= 1:
            return False
        self._stack.pop()
        self._trail.pop()
        return True

    # -- selection -------------------------------------------------------

    def selected_name(self) -> str:
        """The name of the chosen entry, or an empty string when none matches."""
        rows, selected = self._current()
        if not rows:
            return ""
        return rows[min(selected, len(rows) - 1)][0]

    def selected_command(self) -> str:
        """The command the operator has chosen, or empty when none matches."""
        return self.selected_name() if self._open else ""

    def command_line(self) -> str:
        """The full command the operator has chosen, including any descended level.

        This is what submitting the palette runs: choosing ``use`` under ``/model``
        means ``/model use``, which the router already understands.
        """
        if not self._open:
            return ""
        parts = [*self._trail, self.selected_name()]
        return " ".join(part for part in parts if part)

    def rows(self) -> list[CommandRow]:
        """The visible rows of the current level, with the chosen one marked."""
        rows, selected = self._current()
        return [
            CommandRow(name=name, description=description, selected=index == selected)
            for index, (name, description) in enumerate(rows[:MAX_ROWS])
        ]

    def matches(self) -> Sequence[tuple[str, str]]:
        """The rows of the current level."""
        return self._current()[0]

    def breadcrumb(self) -> str:
        """Where the operator is, for the palette's title.

        A deeper level is ambiguous without it: the same ``use`` row means a model
        group here and a provider a level away.
        """
        if not self._open:
            return ""
        return " > ".join(["Commands", *self._trail])

    def hint(self) -> str:
        """The keys the palette answers, shown only while it is open.

        The hint names what ``Enter`` does to the entry currently chosen, because it
        is the one key a reader presses without looking: on a complete command it
        runs it, and on a command that opens a level it opens that level.
        """
        if not self._open:
            return ""
        if self.depth > 1:
            return "Up/Down move · Enter run · Left back · Esc close"
        if self.settles():
            return "Up/Down move · Enter run · Esc close"
        return "Up/Down move · Enter open · Tab complete · Esc close"

    # -- internals -------------------------------------------------------

    def _current(self) -> tuple[tuple[tuple[str, str], ...], int]:
        if not self._stack:
            return (), 0
        return self._stack[-1]

    def _matching(self) -> tuple[tuple[str, str], ...]:
        text = self._typed.strip().lower()
        prefix = text.split()[0] if text else ""
        if not prefix or prefix == "/":
            return COMMANDS
        return tuple(item for item in COMMANDS if item[0].startswith(prefix))
