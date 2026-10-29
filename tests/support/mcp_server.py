"""A real official-SDK server shared by offline stdio and HTTP regressions."""

import asyncio
import os
from pathlib import Path

from mcp.server import MCPServer


def make_server() -> MCPServer[None]:
    server: MCPServer[None] = MCPServer("Cairn test")

    @server.tool()
    async def echo(text: str) -> str:
        """Echo text back; optionally pause to exercise cancellation."""
        if text == "wait":
            await asyncio.sleep(30)
        if text == "secret":
            return os.environ.get("MCP_TEST_SECRET", "absent")
        if text == "ungranted":
            return os.environ.get("MCP_UNGRANTED_SECRET", "absent")
        return text

    return server


if __name__ == "__main__":
    if marker := os.environ.get("MCP_TEST_MARKER"):
        Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    make_server().run()
