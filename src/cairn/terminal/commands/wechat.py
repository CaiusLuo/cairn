"""``/wx``: optional in-session WeChat login and background task service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cairn.terminal.action import Action, correction
from cairn.terminal.commands.context import CommandContext


def handle_wechat(
    context: CommandContext, args: list[str]
) -> tuple[Action | None, Callable[[], Awaitable[Action | None]] | None]:
    if context.wechat_command is None:
        return correction("微信控制只在本机交互式 Cairn 会话中可用。"), None
    text = " ".join(args)
    command = context.wechat_command
    return None, (lambda: command(text))
