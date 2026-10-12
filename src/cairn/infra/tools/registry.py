from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import Tool, ToolExecutionContext, ToolNotFound


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Registered tools as a read-only view; register through register_tool."""
        return MappingProxyType(self._tools)

    def register_tool(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def override_tool(self, tool: Tool) -> None:
        """Replace an already-registered tool under its own name.

        Instrumentation substitutes a recording wrapper for a built-in tool.
        Unlike the private dict this validates the name, so an override cannot
        silently register a tool that was never part of the registry.
        """
        if tool.name not in self._tools:
            raise ToolNotFound(f"Tool not found: {tool.name}")
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Tool:
        tool = self._tools.get(name)

        if tool is None:
            raise ToolNotFound(f"Tool not found: {name}")

        return tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        tool = self.get_tool(name)

        return await tool.execute(arguments, context=context)
