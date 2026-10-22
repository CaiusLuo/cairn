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

Two capabilities can widen the fixed sandbox, and both follow the same rule:
authority comes only from a granted capability, never from a model-provided
argument. ``NETWORK`` lifts the network namespace restriction;
``FILESYSTEM_ACCESS`` adds read/write access to specific absolute directories
outside the workspace. Proving a directory is real and unprotected is a
filesystem effect, so this module delegates it to an injected normalizer and
never touches the filesystem itself.
"""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall


class PermissionCapability(StrEnum):
    NETWORK = "network"
    FILESYSTEM_ACCESS = "filesystem_access"


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
    paths: tuple[str, ...] = ()


class PermissionResult(BaseModel):
    policy_decision: PermissionDecision
    allowed: bool
    prompted: bool = False
    source: PermissionSource = PermissionSource.BASELINE
    granted_capabilities: frozenset[PermissionCapability] = frozenset()
    granted_roots: frozenset[str] = frozenset()
    reason: str = ""


class PermissionHandler(Protocol):
    """Approval handler, consulted only for ASK decisions."""

    def __call__(self, tool_call: ToolCall) -> PermissionResult: ...


class CapabilityPermissionHandler(Protocol):
    """Approve a capability explicitly requested by a trusted harness operation."""

    def authorize(self, request: PermissionRequest) -> PermissionResult: ...


#: Validates one requested root and returns its resolved directory, raising
#: ``ValueError`` when it cannot be granted. Implemented in ``infra``; injected
#: so this module performs no filesystem work.
RootNormalizer = Callable[..., Path]


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


def evaluate_permission_policy(tool_call: ToolCall) -> PermissionResult:
    """Pure baseline policy: intent guardrails only.

    * the sudo guardrail is hard denied before execution;
    * a ``bash`` call that requests a capability asks for approval;
    * every other call is a normal operation inside the fixed sandbox.

    Only ``bash`` requests are visible here because their request lives in the
    tool call arguments. A file tool that reaches outside the workspace reports
    its own request through ``Tool.requested_capability``; the runtime asks the
    tool and calls the handler directly, so it never depends on this function.
    """
    if _is_sudo_guardrail(tool_call):
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
            source=PermissionSource.POLICY_DENY,
            reason="sudo is not supported",
        )

    if _bash_capability_request(tool_call) is not None:
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

    ``workspace_root`` and ``home`` narrow the *deny* checks for a filesystem
    grant: no grant may be, contain, or sit inside them, so the project or the
    user's home is never handed out by naming it or an ancestor. ``root_normalizer``
    performs the path validation (a ``core``-independent ``infra`` effect); it is
    required only if ``FILESYSTEM_ACCESS`` is ever requested.
    """

    def __init__(
        self,
        prompt: Callable[[PermissionRequest], PermissionChoice] | None = None,
        *,
        workspace_root: Path | None = None,
        home: Path | None = None,
        root_normalizer: RootNormalizer | None = None,
    ) -> None:
        self.prompt = prompt
        self.grants: set[PermissionCapability] = set()
        self.granted_roots: set[Path] = set()
        self._root_normalizer = root_normalizer
        self._protected = tuple(
            root for root in (workspace_root, home) if isinstance(root, Path)
        )

    def reset_grants(self) -> None:
        """Drop every session-scoped grant, e.g. after switching providers.

        The global permission policy is untouched: the next ASK decision is
        prompted again instead of reusing a grant made for another provider.
        """
        self.grants.clear()
        self.granted_roots.clear()

    def __call__(self, tool_call: ToolCall) -> PermissionResult:
        """Approve a capability a ``bash`` call requested through its arguments.

        File tools that reach outside the workspace do not go through here; the
        runtime asks their ``Tool.requested_capability`` and calls ``authorize``
        with the parsed request directly.
        """
        if tool_call.name == "bash":
            request = _bash_capability_request(tool_call)
            if request is not None:
                return self.authorize(request)
        return evaluate_permission_policy(tool_call)

    def authorize(self, request: PermissionRequest) -> PermissionResult:
        """Approve an explicit request without changing model tool policy.

        Harness operations construct their own requests after validating their
        inputs. This method never derives authority from a tool name or from
        model-produced arguments; callers must check the granted capability
        before using a network transport or mounting a filesystem root.
        """
        capability = request.capability
        # StrEnum equality also accepts strings; only the actual capability
        # member authorizes, including when model validation was bypassed.
        if capability is PermissionCapability.NETWORK:
            if capability in self.grants:
                return self._granted(capability, source=PermissionSource.SESSION_GRANT)
            return self._prompt_and_grant(request, roots=None)
        if capability is PermissionCapability.FILESYSTEM_ACCESS:
            return self._authorize_filesystem(request)
        raise ValueError("Invalid permission capability")

    def _authorize_filesystem(self, request: PermissionRequest) -> PermissionResult:
        capability = PermissionCapability.FILESYSTEM_ACCESS
        if not request.paths:
            raise ValueError("filesystem request must name at least one path")
        roots = tuple(self._normalize(path) for path in request.paths)

        if all(_covered_by(root, self.granted_roots) for root in roots):
            return self._granted(
                capability, source=PermissionSource.SESSION_GRANT, roots=roots
            )
        return self._prompt_and_grant(request, roots=roots)

    def _prompt_and_grant(
        self, request: PermissionRequest, *, roots: tuple[Path, ...] | None
    ) -> PermissionResult:
        """Prompt once, validate the choice, and grant the requested capability.

        Network passes ``roots=None``; filesystem passes its normalized roots.
        A session grant records them so a later covered request is reused.
        """
        capability = request.capability
        if self.prompt is None:
            return self._no_handler()

        choice = self.prompt(request)
        # Only an actual PermissionChoice member authorizes. StrEnum equality
        # also matches a plain string or a foreign StrEnum, so identity is
        # required; raise before recording any grant so none is half-applied.
        if not any(choice is member for member in PermissionChoice):
            raise ValueError(f"Invalid permission choice: {choice!r}")
        if choice is PermissionChoice.DENY:
            return self._denied()

        if choice is PermissionChoice.ALLOW_SESSION:
            self.grants.add(capability)
            if roots is not None:
                self.granted_roots.update(roots)

        return self._granted(
            capability,
            source=(
                PermissionSource.USER_SESSION
                if choice is PermissionChoice.ALLOW_SESSION
                else PermissionSource.USER_ONCE
            ),
            roots=roots if roots is not None else (),
            prompted=True,
        )

    def _normalize(self, path: str) -> Path:
        if self._root_normalizer is None:
            raise ValueError("filesystem access is unavailable: no root validator")
        return self._root_normalizer(path, protected=self._protected)

    def _granted(
        self,
        capability: PermissionCapability,
        *,
        source: PermissionSource,
        roots: tuple[Path, ...] = (),
        prompted: bool = False,
    ) -> PermissionResult:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=True,
            prompted=prompted,
            source=source,
            granted_capabilities=frozenset({capability}),
            granted_roots=frozenset(str(root) for root in roots),
        )

    def _denied(self) -> PermissionResult:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
            prompted=True,
            source=PermissionSource.USER_DENIED,
        )

    def _no_handler(self) -> PermissionResult:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
            source=PermissionSource.NO_HANDLER,
        )


def _bash_capability_request(tool_call: ToolCall) -> PermissionRequest | None:
    """The capability a ``bash`` call asked for, derived from its arguments.

    Only the bash request lives in the tool call arguments, so only bash is
    derived here. Network and filesystem access are mutually exclusive per call
    (the bash tool rejects a call requesting both), so the order is unambiguous.
    """
    if tool_call.name != "bash":
        return None

    justification = tool_call.arguments.get("justification")
    reason = justification if isinstance(justification, str) else ""

    roots = tool_call.arguments.get("extra_writable_roots")
    if isinstance(roots, list) and roots:
        return PermissionRequest(
            capability=PermissionCapability.FILESYSTEM_ACCESS,
            justification=reason,
            tool_call=tool_call,
            paths=tuple(root for root in roots if isinstance(root, str)),
        )

    if tool_call.arguments.get("network_access") is True:
        return PermissionRequest(
            capability=PermissionCapability.NETWORK,
            justification=reason,
            tool_call=tool_call,
        )
    return None


def _covered_by(resolved: Path, roots: set[Path]) -> bool:
    return any(resolved == root or root in resolved.parents for root in roots)
