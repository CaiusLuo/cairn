from typing import Protocol, Any

from cairn.models import ToolResult

class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]:
        ...

    async def execute(
            self,
            arguments: str 
        ) -> ToolResult:
        ...
