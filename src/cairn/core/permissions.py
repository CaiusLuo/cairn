"""Permission policy, capability approval, and session grants.

Three questions are deliberately kept apart:

* does this tool exist?      -> ``tools/registry.py`` (``ToolNotFound``)
* are its arguments valid?   -> the tool that declares the schema
                                (``InvalidArguments``)
* may it exceed the sandbox? -> this module

The policy below therefore never asks whether a tool is registered and never
inspects whether arguments are well formed. It decides intent guardrails only;
a tool's own implementation enforces what it can, and approval is what grants a
capability the tool would otherwise refuse.

Cairn is local-first, so a shell command runs as the invoking user with no OS
sandbox. ``LOCAL_SHELL`` is therefore a host-level capability: running any
command needs it, and it is asked for explicitly once per session. It cannot be
narrowed to "no network" or "workspace only", because a local process can reach
both; the schema says so instead of implying an isolation this module cannot
enforce.

``NETWORK`` remains for harness operations that perform their own network calls
(GitHub publication). It is not used for shell commands, where it could not be
enforced.
"""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from cairn.core.models import ToolCall


class PermissionCapability(StrEnum):
    #: A harness operation performs its own network request.
    NETWORK = "network"
    #: The user trusts this session with a local, unsandboxed shell.
    LOCAL_SHELL = "local_shell"
    #: The user trusts this session to read and write the Workspace.
    WORKSPACE_TRUST = "workspace_trust"
    #: Write access to specific directories outside the Workspace. Reading
    #: outside the Workspace needs no grant.
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


class CapabilityRequest(BaseModel):
    """One capability a validated tool call asks for.

    A tool derives this from its own arguments during preflight, so the runtime
    can approve a concrete request. ``paths`` is non-empty only for
    ``FILESYSTEM_ACCESS`` and names the directories the call wants to write.
    """

    capability: PermissionCapability
    paths: tuple[str, ...] = ()
    justification: str = ""


class AccessKind(StrEnum):
    """What a tool wants to do with a path outside the Workspace."""

    READ = "read"
    WRITE = "write"


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
    * a ``bash`` call is an ASK, because a local command is a host-level action
      with no sandbox behind it (the handler names the ``LOCAL_SHELL`` capability);
    * every other call is an ordinary tool operation.

    A file tool that needs something outside the workspace reports its own
    request through ``Tool.requested_capability``; the runtime asks the tool and
    calls the handler directly, so it never depends on this function.
    """
    if _is_sudo_guardrail(tool_call):
        return PermissionResult(
            policy_decision=PermissionDecision.DENY,
            allowed=False,
            source=PermissionSource.POLICY_DENY,
            reason="sudo is not supported",
        )

    if tool_call.name == "bash":
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
        """Approve the capability this call needs.

        A ``bash`` call needs ``LOCAL_SHELL``: there is no sandbox, so the shell
        is a host-level action. Every other call is ordinary tool policy. A file
        tool that reaches outside the workspace does not come through here; the
        runtime asks its ``Tool.requested_capability`` and calls ``authorize``
        with the parsed request directly.
        """
        # Policy first: a hard DENY (sudo) must never be re-authorizable by a
        # prompt, and a baseline ALLOW never needs one.
        policy = evaluate_permission_policy(tool_call)
        if policy.policy_decision is not PermissionDecision.ASK:
            # Baseline ALLOW or hard DENY: the handler must not touch either.
            return policy

        if tool_call.name == "bash":
            return self.authorize(
                PermissionRequest(
                    capability=PermissionCapability.LOCAL_SHELL,
                    justification=_shell_justification(tool_call),
                    tool_call=tool_call,
                )
            )
        return policy

    def authorize(self, request: PermissionRequest) -> PermissionResult:
        """Approve an explicit request without changing model tool policy.

        Harness operations construct their own requests after validating their
        inputs. This method never derives authority from a tool name or from
        model-produced arguments; callers must check the granted capability
        before using a network transport or mounting a filesystem root.
        """
        capability = request.capability
        # StrEnum equality also matches a plain string or a foreign StrEnum with
        # the same value, so every branch compares identity. A value that merely
        # equals a member is not that member and cannot reuse a session grant.
        if (
            capability is PermissionCapability.NETWORK
            or (capability is PermissionCapability.LOCAL_SHELL)
            or (capability is PermissionCapability.WORKSPACE_TRUST)
        ):
            if capability in self.grants:
                return self._granted(capability, source=PermissionSource.SESSION_GRANT)
            return self._prompt_and_grant(request, roots=None)
        if capability is PermissionCapability.FILESYSTEM_ACCESS:
            return self._authorize_filesystem(request)
        raise ValueError("Invalid permission capability")

    def ensure_workspace_trust(self) -> PermissionResult:
        """Ask once whether this session may read and write the Workspace.

        A local Cairn edits files in the directory it was started in, so that
        directory is trusted explicitly before the first turn. The grant is
        session-scoped and never written to disk.
        """
        capability = PermissionCapability.WORKSPACE_TRUST
        if capability in self.grants:
            return self._granted(capability, source=PermissionSource.SESSION_GRANT)
        return self._prompt_and_grant(
            PermissionRequest(
                capability=capability,
                justification=(
                    "Read and write files in this directory for the rest of the "
                    "session."
                ),
                tool_call=ToolCall(
                    id="workspace-trust",
                    name="workspace",
                    arguments={},
                ),
            ),
            roots=None,
        )

    def access_decision(
        self, *, outside: bool, access: AccessKind, path: Path
    ) -> PermissionResult:
        """Decide whether a file tool may touch ``path``.

        Inside the Workspace, access follows the session's workspace trust.
        Outside it, reading is allowed and writing needs a grant covering the
        path's directory, which the operator approves once per subtree.
        """
        if not outside:
            if PermissionCapability.WORKSPACE_TRUST in self.grants:
                return PermissionResult(
                    policy_decision=PermissionDecision.ALLOW,
                    allowed=True,
                    source=PermissionSource.SESSION_GRANT,
                )
            return self.ensure_workspace_trust()

        if access is AccessKind.READ:
            return PermissionResult(
                policy_decision=PermissionDecision.ALLOW,
                allowed=True,
                source=PermissionSource.BASELINE,
            )

        if is_within_granted(path, self.granted_roots):
            return self._granted(
                PermissionCapability.FILESYSTEM_ACCESS,
                source=PermissionSource.SESSION_GRANT,
                roots=(path,),
            )
        # The grant is on the containing directory, so approving one file also
        # covers its siblings for the rest of the session.
        return self._authorize_filesystem(
            PermissionRequest(
                capability=PermissionCapability.FILESYSTEM_ACCESS,
                justification=f"Write to {path} outside the Workspace.",
                tool_call=ToolCall(
                    id="filesystem-access",
                    name="write_file",
                    arguments={"path": str(path)},
                ),
                paths=(str(path.parent),),
            )
        )

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


def _shell_justification(tool_call: ToolCall) -> str:
    """The approval text shown when a session asks for local shell access.

    The command is included so the operator approves a concrete action, not an
    abstract capability.
    """
    command = tool_call.arguments.get("command")
    detail = (
        f"Run `{command}`"
        if isinstance(command, str) and command.strip()
        else ("Run a shell command")
    )
    return (
        f"{detail} on this machine as your user. A local shell is not sandboxed: "
        "it can reach the network and any file you can."
    )


def is_within_granted(path: Path, roots: set[Path]) -> bool:
    """Whether ``path`` is already covered by an approved directory grant."""
    return _covered_by(path, roots)


def _covered_by(resolved: Path, roots: set[Path]) -> bool:
    return any(resolved == root or root in resolved.parents for root in roots)
