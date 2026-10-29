"""Real loopback HTTP MCP, with authorization before network effects."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.infra.mcp.config import McpServerConfig
from cairn.runtime.mcp.activation import open_mcp_servers
from tests.support.mcp_server import make_server


@asynccontextmanager
async def http_server() -> AsyncIterator[tuple[str, list[dict[bytes, bytes]]]]:
    app = make_server().streamable_http_app()
    headers: list[dict[bytes, bytes]] = []

    async def record(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers.append(dict(scope["headers"]))
        await app(scope, receive, send)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(record, log_level="critical"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                        raise RuntimeError("test HTTP server did not start")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", headers
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


def test_http_authorizes_then_discovers_calls_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HTTP_AUTH", "Bearer offline-secret")
    handler = SessionPermissionHandler(
        prompt=None, workspace_root=tmp_path, home=tmp_path
    )
    handler.grants.update(
        {PermissionCapability.MCP_SERVER, PermissionCapability.MCP_TOOL}
    )

    async def scenario() -> None:
        async with http_server() as (url, requests):
            config = McpServerConfig(
                name="http",
                transport="streamable_http",
                url=url,
                header_env=(("Authorization", "MCP_HTTP_AUTH"),),
            )
            with pytest.raises(PermissionError):
                async with open_mcp_servers(
                    configs=(config,), handler=handler, workspace=tmp_path
                ):
                    pytest.fail("network was not authorized")
            assert requests == []
            handler.grants.add(PermissionCapability.NETWORK)
            async with open_mcp_servers(
                configs=(config,), handler=handler, workspace=tmp_path
            ) as runtime:
                assert [t.name for t in runtime.tools] == ["mcp__http__echo"]
                result = await runtime.tools[0].execute({"text": "hello over HTTP"})
                assert result.exit_code == 0
                assert result.stdout == "hello over HTTP"
                redacted = await runtime.tools[0].execute(
                    {"text": "Bearer offline-secret"}
                )
                assert redacted.stdout == "[redacted]"
            assert requests
            assert all(
                h.get(b"authorization") == b"Bearer offline-secret" for h in requests
            )
            assert not any(
                t.get_name() == "cairn-mcp-connections" for t in asyncio.all_tasks()
            )

    asyncio.run(scenario())
