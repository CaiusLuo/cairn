import shlex
from collections.abc import Callable, Collection
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall


class PermissionCapability(StrEnum):
    NETWORK = "network"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


class PermissionRequest(BaseModel):
    capability: PermissionCapability
    justification: str
    tool_call: ToolCall


class PermissionResult(BaseModel):
    policy_decision: PermissionDecision
    allowed: bool
    prompted: bool = False
    source: str = "baseline"
    granted_capabilities: frozenset[PermissionCapability] = frozenset()


class PermissionHandler(Protocol):
    def __call__(self, tool_call: ToolCall) -> PermissionResult: ...


def validate_bash_arguments(arguments: dict[str, Any]) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("command must be a non-empty string without NUL")
    if arguments.keys() - {"command", "network_access", "justification"}:
        raise ValueError("unexpected bash command argument")
    if not isinstance(arguments.get("network_access", False), bool):
        raise ValueError("network_access must be a boolean")
    justification = arguments.get("justification", "")
    if not isinstance(justification, str):
        raise ValueError("justification must be a string")
    if arguments.get("network_access", False) and not justification.strip():
        raise ValueError("network_access requires a non-empty justification")
    return command


def check_permission(
    tool_call: ToolCall,
    registered_tools: Collection[str] = ("bash", "read_file", "edit_file"),
) -> PermissionDecision:
    if tool_call.name not in registered_tools:
        return PermissionDecision.DENY
    if tool_call.name != "bash":
        return PermissionDecision.ALLOW
    try:
        command = validate_bash_arguments(tool_call.arguments)
        parts = shlex.split(command)
    except ValueError:
        return PermissionDecision.DENY
    if not parts or parts[0] == "sudo":
        return PermissionDecision.DENY
    if tool_call.arguments.get("network_access", False):
        return PermissionDecision.ASK
    return PermissionDecision.ALLOW


class SessionPermissionHandler:
    def __init__(
        self,
        prompt: Callable[[PermissionRequest], PermissionChoice] | None = None,
        registered_tools: Collection[str] = ("bash", "read_file", "edit_file"),
    ) -> None:
        self.prompt = prompt
        self.registered_tools = registered_tools
        self.grants: set[PermissionCapability] = set()

    def __call__(self, tool_call: ToolCall) -> PermissionResult:
        decision = check_permission(tool_call, self.registered_tools)
        result = PermissionResult(
            policy_decision=decision,
            allowed=decision == PermissionDecision.ALLOW,
            source="hard_deny" if decision == PermissionDecision.DENY else "baseline",
        )
        if decision != PermissionDecision.ASK:
            return result
        capability = PermissionCapability.NETWORK
        if capability in self.grants:
            result.source = "session_grant"
        elif self.prompt is None:
            result.source = "no_handler"
            return result
        else:
            choice = self.prompt(
                PermissionRequest(
                    capability=capability,
                    justification=tool_call.arguments["justification"],
                    tool_call=tool_call,
                )
            )
            result.prompted = True
            if choice == PermissionChoice.DENY:
                result.source = "user_denied"
                return result
            if choice == PermissionChoice.ALLOW_SESSION:
                self.grants.add(capability)
                result.source = "session_grant"
            else:
                result.source = "user_once"
        result.allowed = True
        result.granted_capabilities = frozenset({capability})
        return result
