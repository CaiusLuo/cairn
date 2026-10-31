"""``/memory``: personal AGENT.md and daily memory commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cairn.terminal.action import Action, correction
from cairn.terminal.commands.context import CommandContext


def handle_memory(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    if context.memory_command is None:
        return correction("个人记忆只在本机交互式 Cairn 会话中可用。"), None
    text = " ".join(args)
    command = context.memory_command
    return None, (lambda: command(text))
