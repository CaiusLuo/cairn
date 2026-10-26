"""Where a typed line goes: a command, or a turn.

The router's job is to decide that, and to say what the operator should see about
the decision. It owns neither the transcript nor the bottom surface: a command that
has something to say returns an :class:`~cairn.terminal.action.Action`, and the
caller decides where to show it. In a terminal that is the row above the composer;
in a pipeline it is plain text.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cairn.terminal.action import Action, correction
from cairn.terminal.commands.compact import handle_compact
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.help import handle_help
from cairn.terminal.commands.model import handle_model
from cairn.terminal.commands.provider import handle_provider
from cairn.terminal.commands.trace import handle_trace


@dataclass
class CommandResult:
    handled: bool
    should_exit: bool = False
    # Input interactions are awaited by the CLI; ordinary commands stay synchronous.
    interaction: Callable[[], Awaitable[Action | None]] | None = None
    #: What the operator should see about this command. ``None`` means the command
    #: printed its own output (explicit `/help`, `/trace`, `/model list`), which is
    #: what those commands exist to do.
    action: Action | None = None


class CommandRouter:
    def handle(
        self,
        raw: str,
        context: CommandContext,
    ) -> CommandResult:
        if not raw.startswith("/"):
            return CommandResult(handled=False)

        parts = raw.split()
        command = parts[0].lower()
        args = parts[1:]

        if command == "/exit":
            return CommandResult(handled=True, should_exit=True)

        if command == "/trace":
            handle_trace(context, args)
            return CommandResult(handled=True)

        if command == "/model":
            action, interaction = handle_model(context, args)
            return CommandResult(handled=True, action=action, interaction=interaction)

        if command == "/provider":
            action, interaction = handle_provider(context, args)
            return CommandResult(handled=True, action=action, interaction=interaction)

        if command == "/compact":
            return CommandResult(
                handled=True,
                interaction=handle_compact(context.compact_now),
            )

        if command == "/help":
            handle_help(args)
            return CommandResult(handled=True)

        # A wrong command is corrected, not answered with the manual. The correction
        # goes to the temporary surface above the composer: printing it into the
        # transcript left a line about a typo in the conversation for the rest of the
        # session, and the operator had already moved on by the time it appeared.
        return CommandResult(
            handled=True,
            action=correction(
                f"{parts[0]} is not an in-session command. "
                "Type / to browse, or /help for the list."
            ),
        )
