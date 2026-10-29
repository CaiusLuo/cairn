"""The SDK boundary preserves paginated catalogs and request time limits."""

import asyncio
from typing import Any

import pytest
from mcp.types import ListToolsResult, Tool

from cairn.infra.mcp.client import McpClient


def test_tool_discovery_follows_pages_and_rejects_cursor_loops() -> None:
    class Catalog:
        loop = False

        async def list_tools(self, *, cursor: str | None) -> ListToolsResult:
            if cursor is None:
                return ListToolsResult(
                    tools=[Tool(name="first", input_schema={"type": "object"})],
                    next_cursor="page-2",
                )
            assert cursor == "page-2"
            return ListToolsResult(
                tools=[Tool(name="second", input_schema={"type": "object"})],
                next_cursor="page-2" if self.loop else None,
            )

    catalog = Catalog()
    client = McpClient(catalog, timeout=1, secrets=())  # type: ignore[arg-type]
    assert [t.name for t in asyncio.run(client.list_tools())] == ["first", "second"]
    catalog.loop = True
    with pytest.raises(ValueError, match="pagination"):
        asyncio.run(client.list_tools())


def test_tool_deadline_cancels_the_request() -> None:
    cancelled = False

    class Hung:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            nonlocal cancelled
            try:
                await asyncio.sleep(30)
            finally:
                cancelled = True

    client = McpClient(Hung(), timeout=0.01, secrets=())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        asyncio.run(client.call_tool("slow", {}))
    assert cancelled
