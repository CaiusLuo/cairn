from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
    interaction: Callable[[], Awaitable[None]] | None = None


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

        if command in {"/exit", "/quit"}:
            return CommandResult(handled=True, should_exit=True)

        if command == "/trace":
            handle_trace(context, args)
            return CommandResult(handled=True)

        if command == "/model":
            return CommandResult(handled=True, interaction=handle_model(context, args))

        if command == "/provider":
            handle_provider(context, args)
            return CommandResult(handled=True)

        if command == "/compact":
            return CommandResult(
                handled=True, interaction=handle_compact(context.compact_now)
            )

        if command == "/help":
            handle_help(args)
            return CommandResult(handled=True)

        # A wrong command is corrected, not answered with the manual. Printing every
        # command buried the conversation for a typo; the operator learns more from
        # one line naming what they typed and how to see the list.
        print(
            f"{parts[0]} is not an in-session command. Type /help to see the commands."
        )
        return CommandResult(handled=True)
