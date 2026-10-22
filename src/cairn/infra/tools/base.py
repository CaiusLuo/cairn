from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from cairn.core.models import ToolResult


class ToolNotFound(ValueError):
    """The registry has no tool under the requested name.

    Tool existence is owned by the registry, not by the permission policy, so
    this is a tool failure rather than an authorization outcome.
    """


class InvalidArguments(ValueError):
    """The tool call arguments violate the tool's own input contract.

    Argument validity is owned by the tool that declares the schema, not by the
    permission policy, so this is a tool failure rather than an authorization
    outcome.
    """


class ToolFailureKind(StrEnum):
    """Effect-aware outcome of one failed tool operation.

    Only ``transient_before_effect`` can be considered for automatic retry, and
    only by an implementation that independently proves its operation is safe to
    repeat. The type is diagnostic for every other failure; it grants no retry
    authority to the generic tool runner.
    """

    TRANSIENT_BEFORE_EFFECT = "transient_before_effect"
    PERMANENT = "permanent"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    EFFECT_UNKNOWN = "effect_unknown"


@dataclass(frozen=True, slots=True)
class ToolFailureOutcome:
    """Sanitized typed failure from an implementation-owned tool attempt."""

    kind: ToolFailureKind
    error_type: str
    message: str


class ToolExecutionFailure(RuntimeError):
    """Carry a typed tool outcome without changing the model-facing error."""

    def __init__(self, outcome: ToolFailureOutcome) -> None:
        self.outcome = outcome
        super().__init__(outcome.message)


@dataclass(frozen=True)
class ToolExecutionContext:
    """Internal authority granted to a single tool execution.

    It is built by the agent loop from an approved permission result and is the
    only channel through which a tool learns that it may exceed the sandbox.
    Model-provided arguments are never trusted as proof of approval.

    ``accessible_roots`` holds the normalized absolute directories a
    ``FILESYSTEM_ACCESS`` grant approved for this execution. It is empty for an
    ordinary call, so a file tool outside the workspace with no grant stays
    refused. A local shell needs no per-execution authority: it is approved once
    per session, before any command runs.
    """

    accessible_roots: tuple[Path, ...] = ()


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]: ...

    def validate(self, arguments: dict[str, Any]) -> None:
        """Check the input contract without side effects; raise InvalidArguments."""
        ...

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult: ...


class WorkspaceOnlyTool:
    """A tool that cannot leave the Workspace.

    ``requested_capability`` is an optional member, not part of the ``Tool``
    protocol: only a tool that can reach outside the Workspace reports one, and
    the runtime asks for it defensively. This base class documents that intent
    for the tools that opt in by inheriting it.
    """

    def requested_capability(self, arguments: dict[str, Any]) -> None:
        """This tool asks for no capability beyond the workspace itself."""
        return None
