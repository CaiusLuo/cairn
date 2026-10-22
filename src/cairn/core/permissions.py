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
argument. ``NETWORK`` lifts the network namespace restriction; ``FILESYSTEM_ACCESS``
adds read/write access to specific absolute directories outside the workspace.
A filesystem grant is a set of normalized, per-session roots; the tool receives
them only after approval and re-validates every target against them.
"""

import os
from collections.abc import Callable, Collection, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall

#: Absolute directories that may never be granted, even with approval. These
#: are either the filesystem root, a home/system tree that would expose
#: credentials or the whole machine, or a pseudo-filesystem. A grant is scoped
#: to a project-adjacent data directory, never to one of these.
_BLOCKED_SYSTEM_ROOTS: frozenset[Path] = frozenset(
    {
        Path("/"),
        Path("/home"),
        Path("/root"),
        Path("/Users"),
        Path("/etc"),
        Path("/usr"),
        Path("/lib"),
        Path("/lib32"),
        Path("/lib64"),
        Path("/bin"),
        Path("/sbin"),
        Path("/boot"),
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
    }
)


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


class CapabilityRequest(BaseModel):
    """One capability a validated tool call asks for.

    ``paths`` is non-empty only for ``FILESYSTEM_ACCESS`` and holds the absolute
    directories the call wants to reach outside the workspace. The tool derives
    them during its own preflight; they are never read from a trusted context.
    """

    capability: PermissionCapability
    paths: tuple[str, ...] = ()
    justification: str = ""


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


def _bash_extra_roots(tool_call: ToolCall) -> tuple[str, ...]:
    """Absolute extra roots a bash call declared, or an empty tuple.

    Shape was already validated by the tool; this only extracts the strings so
    the policy can see that a filesystem capability is being requested.
    """
    raw = tool_call.arguments.get("extra_writable_roots")
    if not isinstance(raw, list):
        return ()
    return tuple(value for value in raw if isinstance(value, str) and value)


def requested_capability(tool_call: ToolCall) -> CapabilityRequest | None:
    """Return the capability a bash call requests, or ``None``.

    Only the core-visible bash capabilities are derived here because only their
    request lives in the tool call arguments. A file tool that reaches outside
    the workspace reports its own request through the tool protocol, which the
    runtime merges with this result.
    """
    if tool_call.name != "bash":
        return None

    if tool_call.arguments.get("network_access") is True:
        return CapabilityRequest(
            capability=PermissionCapability.NETWORK,
            justification=_justification(tool_call),
        )

    roots = _bash_extra_roots(tool_call)
    if roots:
        return CapabilityRequest(
            capability=PermissionCapability.FILESYSTEM_ACCESS,
            paths=roots,
            justification=_justification(tool_call),
        )

    return None


def _justification(tool_call: ToolCall) -> str:
    raw = tool_call.arguments.get("justification")
    return raw if isinstance(raw, str) else ""


def evaluate_permission_policy(
    tool_call: ToolCall,
    request: CapabilityRequest | None = None,
) -> PermissionResult:
    """Pure baseline policy: intent guardrails only.

    * the sudo guardrail is hard denied before execution;
    * an explicit capability request (from the tool protocol, or derived from a
      bash call) asks for approval;
    * every other call is a normal operation inside the fixed sandbox.

    ``request`` lets the runtime hand in a tool-owned request that core cannot
    derive itself (a file tool reaching outside the workspace). When it is
    omitted the policy falls back to the bash-visible request.
    """
    if _is_sudo_guardrail(tool_call):
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
            source=PermissionSource.POLICY_DENY,
            reason="sudo is not supported",
        )

    if request is None:
        request = requested_capability(tool_call)

    if request is not None:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
        )

    return PermissionResult(
        policy_decision=PermissionDecision.ALLOW,
        allowed=True,
    )


def normalize_granted_root(
    raw: object,
    *,
    protected: Iterable[Path] = (),
) -> Path:
    """Validate one requested filesystem root and return its normalized form.

    A root must be an absolute path to an existing directory that is not a
    blocked system tree and does not contain ``.git``. Symlinks anywhere along
    it are rejected because the mount source would resolve somewhere the checks
    never inspected. ``protected`` adds caller-specific forbidden roots (the
    workspace and the user's home), including their ancestors.

    Raises:
        ValueError: the value cannot be granted.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("filesystem path must be a non-empty string")
    if os.sep != "/":
        # The sandbox implementation this capability targets is POSIX-only.
        raise ValueError("filesystem capability requires a POSIX path")

    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("filesystem path must be absolute")

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("filesystem path does not exist") from None

    if not resolved.is_dir():
        raise ValueError("filesystem path must be a directory")

    if resolved in _BLOCKED_SYSTEM_ROOTS:
        raise ValueError("filesystem path is a protected system location")

    for forbidden in protected:
        # Reject a grant that is, contains, or sits inside a protected root, so
        # neither the project nor the user's home is ever handed out wholesale
        # and no grant is smuggled in by naming one of their ancestors.
        if (
            resolved == forbidden
            or forbidden in resolved.parents
            or resolved in forbidden.parents
        ):
            raise ValueError("filesystem path overlaps a protected root")

    if any(part.casefold() == ".git" for part in resolved.parts):
        raise ValueError("filesystem path may not include Git metadata")

    return resolved


def _covered_by(resolved: Path, roots: Collection[Path]) -> bool:
    return any(resolved == root or root in resolved.parents for root in roots)


class SessionPermissionHandler:
    """Stateful capability approval for ASK decisions.

    Session grants live here, in core permission state: never in the UI and
    never on disk. A fresh instance (a fresh Cairn process) starts with no
    grants. Baseline ALLOW and hard DENY never reach this handler.

    ``workspace_root`` and ``home`` are optional and only widen the *deny*
    checks: when supplied, no grant may contain them (or an ancestor), so a
    filesystem approval can never hand out the project or the user's home by
    mistake. Production passes both; a bare handler still enforces the fixed
    system-root denials.
    """

    def __init__(
        self,
        prompt: Callable[[PermissionRequest], PermissionChoice] | None = None,
        *,
        workspace_root: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self.prompt = prompt
        self.grants: set[PermissionCapability] = set()
        self.granted_roots: set[Path] = set()
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
        request = requested_capability(tool_call)
        if request is None:
            return evaluate_permission_policy(tool_call)

        return self.authorize(
            PermissionRequest(
                capability=request.capability,
                justification=request.justification,
                tool_call=tool_call,
                paths=request.paths,
            )
        )

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
            return self._authorize_network(request)
        if capability is PermissionCapability.FILESYSTEM_ACCESS:
            return self._authorize_filesystem(request)
        raise ValueError("Invalid permission capability")

    def _authorize_network(self, request: PermissionRequest) -> PermissionResult:
        capability = PermissionCapability.NETWORK
        if capability in self.grants:
            return self._granted(capability, source=PermissionSource.SESSION_GRANT)

        if self.prompt is None:
            return self._no_handler()

        choice = self.prompt(request)
        self._record_choice(capability, choice)
        if choice is PermissionChoice.DENY:
            return self._denied()
        if choice is PermissionChoice.ALLOW_SESSION:
            self.grants.add(capability)
        return self._granted(capability, source=self._source_for(choice), prompted=True)

    def _authorize_filesystem(self, request: PermissionRequest) -> PermissionResult:
        capability = PermissionCapability.FILESYSTEM_ACCESS

        if not request.paths:
            raise ValueError("filesystem request must name at least one path")
        roots = tuple(
            normalize_granted_root(path, protected=self._protected)
            for path in request.paths
        )

        if all(_covered_by(root, self.granted_roots) for root in roots):
            return self._granted(
                capability,
                source=PermissionSource.SESSION_GRANT,
                roots=roots,
            )

        if self.prompt is None:
            return self._no_handler()

        choice = self.prompt(request)
        self._record_choice(capability, choice)
        if choice is PermissionChoice.DENY:
            return self._denied()

        if choice is PermissionChoice.ALLOW_SESSION:
            self.granted_roots.update(roots)
            scope = tuple(sorted(self.granted_roots))
        else:
            scope = roots

        return self._granted(
            capability,
            source=self._source_for(choice),
            roots=scope,
            prompted=True,
        )

    def filesystem_roots(self) -> tuple[Path, ...]:
        """Directories approved for this session, for a caller that replays them.

        The agent loop consults this when a tool re-validates a call it has
        already received approval for; it is never read from model arguments.
        """
        return tuple(sorted(self.granted_roots))

    def _record_choice(
        self, capability: PermissionCapability, choice: PermissionChoice
    ) -> None:
        # Only an actual PermissionChoice member authorizes. StrEnum equality
        # also matches a plain string or a foreign StrEnum with the same value,
        # so identity is required here; an unknown value raises before any
        # grant is recorded and can never leave a half-applied grant behind.
        if not any(choice is member for member in PermissionChoice):
            raise ValueError(f"Invalid permission choice: {choice!r}")

    def _source_for(self, choice: PermissionChoice) -> PermissionSource:
        return (
            PermissionSource.USER_SESSION
            if choice is PermissionChoice.ALLOW_SESSION
            else PermissionSource.USER_ONCE
        )

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
