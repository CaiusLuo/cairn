import asyncio
from typing import Any

import pytest

from cairn.core.models import ToolResult
from cairn.tools.base import Tool
from cairn.tools.registry import ToolRegistry


class EchoTool:
    name = "echo"
    description = "Return the supplied value."

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(stdout=str(arguments["value"]), exit_code=0)


def test_duplicate_tool_raises() -> None:
    registry = ToolRegistry()
    tool: Tool = EchoTool()

    registry.register_tool(tool)

    with pytest.raises(ValueError, match="Tool already registered: echo"):
        registry.register_tool(tool)


def test_get_missing_tool_raises() -> None:
    with pytest.raises(ValueError, match="Tool not found: missing"):
        ToolRegistry().get_tool("missing")


def test_registry_exposes_schemas_and_executes_tools() -> None:
    registry = ToolRegistry()
    tool: Tool = EchoTool()
    registry.register_tool(tool)

    result = asyncio.run(
        registry.execute(
            "echo",
            {"value": "hello"},
        )
    )

    assert registry.get_tool("echo") is tool
    assert registry.schemas() == [{"name": "echo"}]
    assert result == ToolResult(stdout="hello", exit_code=0)
