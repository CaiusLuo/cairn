from dataclasses import dataclass
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


@dataclass(frozen=True)
class ToolExecutionContext:
    """Internal authority granted to a single tool execution.

    It is built by the agent loop from an approved permission result and is the
    only channel through which a tool learns that it may exceed the sandbox.
    Model-provided arguments are never trusted as proof of approval.
    """

    network_access: bool = False


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
