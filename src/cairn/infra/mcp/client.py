"""Official MCP SDK transports; runtime owns activation and connection lifetime."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from cairn.infra.mcp.config import McpServerConfig

MAX_DISCOVERY_PAGES = 100


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpClient:
    def __init__(
        self, sdk: Client, *, timeout: float, secrets: tuple[str, ...]
    ) -> None:
        self.sdk = sdk
        self.timeout = timeout
        self.secret_values = secrets

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        specs: list[McpToolSpec] = []
        cursor: str | None = None
        cursors: set[str] = set()
        async with asyncio.timeout(self.timeout):
            for _ in range(MAX_DISCOVERY_PAGES):
                page = await self.sdk.list_tools(cursor=cursor)
                specs.extend(
                    McpToolSpec(tool.name, tool.description or "", tool.input_schema)
                    for tool in page.tools
                )
                cursor = page.next_cursor
                if cursor is None:
                    return tuple(specs)
                if cursor in cursors:
                    break
                cursors.add(cursor)
        raise ValueError("MCP tool discovery exceeded its pagination limit")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with asyncio.timeout(self.timeout):
            result = await self.sdk.call_tool(name, arguments)
        return result.model_dump(by_alias=True, exclude_none=True)


@asynccontextmanager
async def open_mcp_client(
    config: McpServerConfig, *, workspace: Path, timeout: float
) -> AsyncIterator[McpClient]:
    """Enter and exit in one owning task, after runtime grants capabilities.

    The SDK supplies protocol negotiation, notifications, pagination envelopes,
    process-tree termination and HTTP session deletion. No callback grants a
    server access to model sampling, roots or interactive credentials.
    """
    keys = (*config.env_keys, *(key for _, key in config.header_env))
    missing = [key for key in keys if key not in os.environ]
    if missing:
        raise ValueError("MCP host environment is missing configured variable names")
    secrets = tuple(os.environ[key] for key in keys if os.environ[key])
    async with AsyncExitStack() as stack:
        if config.transport == "stdio":
            if config.command is None:
                raise ValueError("stdio MCP requires a command")
            # Never inherit arbitrary host credentials or forward raw child stderr.
            errlog = stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115
            transport = stdio_client(
                StdioServerParameters(
                    command=config.command,
                    args=list(config.args),
                    env={key: os.environ[key] for key in config.env_keys},
                    cwd=workspace,
                ),
                errlog=errlog,
            )
        elif config.transport == "streamable_http":
            if config.url is None:
                raise ValueError("HTTP MCP requires a URL")
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={name: os.environ[key] for name, key in config.header_env},
                    timeout=httpx2.Timeout(timeout),
                    trust_env=False,
                )
            )
            transport = streamable_http_client(config.url, http_client=http)
        else:
            raise ValueError("Unsupported MCP transport")
        async with asyncio.timeout(timeout):
            sdk = await stack.enter_async_context(
                Client(transport, read_timeout_seconds=timeout, cache=None)
            )
        yield McpClient(sdk, timeout=timeout, secrets=secrets)
