import shlex
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall

SAFE_COMMANDS = {"pwd", "ls"}

DENY_COMMANDS = {
    "sudo",
}


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionResult(BaseModel):
    policy_decision: PermissionDecision
    allowed: bool
    prompted: bool = False


class PermissionHandler(Protocol):
    def __call__(
        self,
        tool_call: ToolCall,
    ) -> PermissionResult: ...


def check_permission(tool_call: ToolCall) -> PermissionDecision:
    if tool_call.name != "bash":
        return PermissionDecision.ASK

    command = tool_call.arguments.get("command")

    if not isinstance(command, str) or not command.strip():
        return PermissionDecision.DENY

    # Only exact commands are safe to pass through a shell without prompting.
    if command.strip() in SAFE_COMMANDS:
        return PermissionDecision.ALLOW

    try:
        part = shlex.split(command)
    except ValueError:
        return PermissionDecision.DENY

    if part and part[0] in DENY_COMMANDS:
        return PermissionDecision.DENY

    return PermissionDecision.ASK
