import shlex

from enum import Enum
from typing import Protocol

from cairn.core.models import ToolCall

SAFE_COMMANDS = {
    "pwd",
    "ls",
    "cat",
    "head",
    "tail",
    "rg",
    "grep",
}

DENY_COMMANDS = {
    "sudo",
}

class PermissionDecision(str ,Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

class PermissionHandler(Protocol):
    def __call__(
            self,
            tool_call: ToolCall,
    ) -> PermissionDecision:
        ...

def check_permission(
        tool_call: ToolCall
    ) -> PermissionDecision:
    if tool_call.name != "bash":
        return PermissionDecision.ASK

    command = tool_call.arguments.get("command")

    if not command:
        return PermissionDecision.DENY

    # 有 shell 组合操作时先不自动放行
    shell_operators = ["&&", "||", ";", "|", ">", "<", "$(", "`"]

    if any(op in command for op in shell_operators):
        return PermissionDecision.ASK

    try:
        part = shlex.split(command)
    except ValueError:
        return PermissionDecision.DENY

    if not part:
        return PermissionDecision.DENY

    executable = part[0]

    if executable in SAFE_COMMANDS:
        return PermissionDecision.ALLOW
    
    if executable in DENY_COMMANDS:
        return PermissionDecision.DENY

    if executable == "git" and len(part) >= 2:
        if part[1] in {
            "status",
            "diff",
            "log",
            "show",
        }:
            return PermissionDecision.ALLOW
        
    return PermissionDecision.ASK