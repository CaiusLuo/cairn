from typing import Any

from cairn.core.models import ToolResult
from cairn.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register_tool(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self.tools[tool.name] = tool

    def get_tool(self, name: str) -> Tool:
        tool = self.tools.get(name)

        if tool is None:
            raise ValueError(f"Tool not found: {name}")

        return tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.get_tool(name)

        return await tool.execute(arguments)
