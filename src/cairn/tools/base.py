from dataclasses import dataclass
from typing import Any, Protocol

from cairn.core.models import ToolResult


@dataclass(frozen=True)
class ToolExecutionContext:
    network_access: bool = False


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]: ...

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult: ...
