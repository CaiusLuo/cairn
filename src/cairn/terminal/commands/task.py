"""``/task``: shared task-board administration and inspection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cairn.terminal.action import Action, correction
from cairn.terminal.commands.context import CommandContext


def handle_task(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    if context.task_command is None:
        return correction("共享任务面板只在本机交互式 Cairn 会话中可用。"), None
    command = context.task_command
    return None, (lambda: command(" ".join(args)))
