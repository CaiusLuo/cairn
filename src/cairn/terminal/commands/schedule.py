"""``/schedule``: host-authored scheduled tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cairn.terminal.action import Action, correction
from cairn.terminal.commands.context import CommandContext


def handle_schedule(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    if context.schedule_command is None:
        return correction("定时任务只在本机交互式 Cairn 会话中可用。"), None
    text = " ".join(args)
    command = context.schedule_command
    return None, (lambda: command(text))
