from dataclasses import dataclass

from cairn.commands.context import CommandContext
from cairn.commands.help import handle_help
from cairn.commands.trace import handle_trace


@dataclass
class CommandResult:
    handled: bool
    should_exit: bool = False


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

        if command == "/help":
            handle_help(args)
            return CommandResult(handled=True)

        print(f"Unknown command: {parts[0]}")
        return CommandResult(handled=True)
