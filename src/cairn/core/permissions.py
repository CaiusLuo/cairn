"""Permission policy, capability approval, and session grants.

Three questions are deliberately kept apart:

* does this tool exist?      -> ``tools/registry.py`` (``ToolNotFound``)
* are its arguments valid?   -> the tool that declares the schema
                                (``InvalidArguments``)
* may it exceed the sandbox? -> this module

The policy below therefore never asks whether a tool is registered and never
inspects whether arguments are well formed. It decides intent guardrails only;
the sandbox enforces authority, and approval can expand authority by one
capability at a time.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall


class PermissionCapability(StrEnum):
    NETWORK = "network"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"


class PermissionSource(StrEnum):
    """Decision provenance for traces and model-facing denial semantics.

    Authority control branches on PermissionDecision and granted capabilities,
    never on this value.
    """

    BASELINE = "baseline"
    POLICY_DENY = "policy_deny"
    USER_ONCE = "user_once"
    USER_SESSION = "user_session"
    SESSION_GRANT = "session_grant"
    USER_DENIED = "user_denied"
    NO_HANDLER = "no_handler"


class PermissionRequest(BaseModel):
    capability: PermissionCapability
    justification: str
    tool_call: ToolCall


class PermissionResult(BaseModel):
    policy_decision: PermissionDecision
    allowed: bool
    prompted: bool = False
    source: PermissionSource = PermissionSource.BASELINE
    granted_capabilities: frozenset[PermissionCapability] = frozenset()
    reason: str = ""


class PermissionHandler(Protocol):
    """Approval handler, consulted only for ASK decisions."""

    def __call__(self, tool_call: ToolCall) -> PermissionResult: ...


def _is_sudo_guardrail(tool_call: ToolCall) -> bool:
    """Best-effort action guardrail for commands whose first token is ``sudo``.

    The sandbox, not command parsing, is the security boundary. This must stay
    narrow, must never raise, and deliberately ignores shell indirection such as
    ``/usr/bin/sudo``, ``sh -c 'sudo ...'`` or ``true; sudo ...``. Do not grow it
    into a shell parser.
    """
    if tool_call.name != "bash":
        return False

    command = tool_call.arguments.get("command")
    if not isinstance(command, str):
        return False

    stripped = command.strip()
    return bool(stripped) and stripped.split(maxsplit=1)[0] == "sudo"


def requested_capability(tool_call: ToolCall) -> PermissionCapability | None:
    """Return the capability requested by a tool call already validated by its tool."""
    if tool_call.name != "bash":
        return None

    if tool_call.arguments.get("network_access") is not True:
        return None

    return PermissionCapability.NETWORK


def evaluate_permission_policy(tool_call: ToolCall) -> PermissionResult:
    """Pure baseline policy: intent guardrails only.

    * the sudo guardrail is hard denied before execution;
    * a validated capability request asks for approval;
    * every other call is a normal operation inside the fixed sandbox.
    """
    if _is_sudo_guardrail(tool_call):
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
            source=PermissionSource.POLICY_DENY,
            reason="sudo is not supported",
        )

    if requested_capability(tool_call) is not None:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
        )

    return PermissionResult(
        policy_decision=PermissionDecision.ALLOW,
        allowed=True,
    )


class SessionPermissionHandler:
    """Stateful capability approval for ASK decisions.

    Session grants live here, in core permission state: never in the UI and
    never on disk. A fresh instance (a fresh Cairn process) starts with no
    grants. Baseline ALLOW and hard DENY never reach this handler.
    """

    def __init__(
        self,
        prompt: Callable[[PermissionRequest], PermissionChoice] | None = None,
    ) -> None:
        self.prompt = prompt
        self.grants: set[PermissionCapability] = set()

    def __call__(self, tool_call: ToolCall) -> PermissionResult:
        capability = requested_capability(tool_call)
        if capability is None:
            return evaluate_permission_policy(tool_call)

        if capability in self.grants:
            return PermissionResult(
                policy_decision=PermissionDecision.ASK,
                allowed=True,
                source=PermissionSource.SESSION_GRANT,
                granted_capabilities=frozenset({capability}),
            )

        if self.prompt is None:
            return PermissionResult(
                policy_decision=PermissionDecision.ASK,
                allowed=False,
                source=PermissionSource.NO_HANDLER,
            )

        choice = self.prompt(
            PermissionRequest(
                capability=capability,
                justification=tool_call.arguments["justification"],
                tool_call=tool_call,
            )
        )

        if choice == PermissionChoice.DENY:
            return PermissionResult(
                policy_decision=PermissionDecision.ASK,
                allowed=False,
                prompted=True,
                source=PermissionSource.USER_DENIED,
            )

        if choice == PermissionChoice.ALLOW_SESSION:
            self.grants.add(capability)
            source = PermissionSource.USER_SESSION
        else:
            source = PermissionSource.USER_ONCE

        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=True,
            prompted=True,
            source=source,
            granted_capabilities=frozenset({capability}),
        )
