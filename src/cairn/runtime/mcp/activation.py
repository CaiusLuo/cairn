"""Activate configured MCP servers for one task or turn.

Starting a stdio server executes a host process and a streamable-http server
opens a network connection, so **authorization happens before the process or
connection exists**: MCP_SERVER plus LOCAL_SHELL (stdio) or NETWORK (http). Only
after that do we start the server and discover tools. The adapters are exposed
only for this task, and every call reports ``requires_authorization`` so the
runtime asks for MCP_TOOL instead of inheriting the baseline ALLOW.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.core.models import ToolCall, ToolResult
from cairn.core.permissions import (
    CapabilityRequest,
    PermissionCapability,
    PermissionHandler,
    PermissionRequest,
)
from cairn.infra.mcp.config import McpServerConfig
from cairn.infra.mcp.stdio import McpStdioClient, McpToolSpec
from cairn.infra.tools.base import InvalidArguments


def _redact(text: str, secret_values: Sequence[str]) -> str:
    for value in secret_values:
        if value:
            text = text.replace(value, "[redacted]")
    return text


class McpToolAdapter:
    """One server tool exposed to the model under a namespaced name."""

    requires_authorization = True

    def __init__(
        self,
        *,
        client: McpStdioClient,
        server: str,
        spec: McpToolSpec,
        secret_values: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.server = server
        self.spec = spec
        self.secret_values = tuple(secret_values)
        self.name = f"mcp__{server}__{spec.name}"
        self.description = spec.description or f"MCP tool {spec.name} on {server}"

    def schema(self) -> dict[str, Any]:
        parameters = self.spec.input_schema or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise InvalidArguments("MCP tool arguments must be an object")
        schema = self.spec.input_schema or {}
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list):
            missing = [key for key in required if key not in arguments]
            if missing:
                raise InvalidArguments(
                    f"missing required argument(s): {', '.join(missing)}"
                )
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(properties, dict):
            for key, value in arguments.items():
                prop = properties.get(key)
                if not isinstance(prop, dict):
                    continue
                expected = prop.get("type")
                if expected == "string" and not isinstance(value, str):
                    raise InvalidArguments(f"argument '{key}' must be a string")
                if expected == "integer" and not isinstance(value, int):
                    raise InvalidArguments(f"argument '{key}' must be an integer")
                if expected == "boolean" and not isinstance(value, bool):
                    raise InvalidArguments(f"argument '{key}' must be a boolean")

    def requested_capability(self, arguments: dict[str, Any]) -> CapabilityRequest:
        return CapabilityRequest(
            capability=PermissionCapability.MCP_TOOL,
            justification=f"Call MCP tool '{self.spec.name}' on server '{self.server}'.",
        )

    async def execute(
        self, arguments: dict[str, Any], *, context: object | None = None
    ) -> ToolResult:
        try:
            result = await self.client.call_tool(self.spec.name, arguments)
        except TimeoutError:
            return ToolResult(stdout="", stderr="MCP tool timed out", exit_code=1)
        except Exception as exc:
            return ToolResult(
                stdout="",
                stderr=_redact(
                    f"MCP tool error: {type(exc).__name__}", self.secret_values
                ),
                exit_code=1,
            )
        content = result.get("content", [])
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
        output = _redact("\n".join(parts), self.secret_values)
        is_error = bool(result.get("isError"))
        return ToolResult(
            stdout=output,
            stderr="MCP tool reported an error" if is_error else "",
            exit_code=1 if is_error else 0,
        )


@dataclass(frozen=True, slots=True)
class McpRuntime:
    tools: tuple[McpToolAdapter, ...]
    servers: tuple[str, ...]


async def _authorize_server(
    handler: PermissionHandler | None, config: McpServerConfig
) -> None:
    if handler is None:
        raise PermissionError("MCP activation requires a permission handler")
    capabilities = [PermissionCapability.MCP_SERVER, PermissionCapability.MCP_TOOL]
    if config.transport == "stdio":
        capabilities.append(PermissionCapability.LOCAL_SHELL)
    else:
        capabilities.append(PermissionCapability.NETWORK)
    for capability in capabilities:
        request = PermissionRequest(
            capability=capability,
            justification=(
                f"Use MCP server '{config.name}' ({config.transport}) for this task."
            ),
            tool_call=ToolCall(
                id=f"mcp-{config.name}", name="mcp", arguments={"server": config.name}
            ),
        )
        authorize_async = getattr(handler, "authorize_async", None)
        if callable(authorize_async):
            result = await authorize_async(request)
        else:
            authorize = getattr(handler, "authorize", None)
            if not callable(authorize):
                raise PermissionError("permission handler cannot authorize MCP")
            result = authorize(request)
        if not result.allowed:
            raise PermissionError(f"MCP server '{config.name}' was not authorized")


@asynccontextmanager
async def open_mcp_servers(
    *,
    configs: Sequence[McpServerConfig],
    handler: PermissionHandler | None,
    workspace: Path,
    secret_values: Sequence[str] = (),
    timeout: float = 10.0,
) -> AsyncIterator[McpRuntime]:
    """Authorize, start and discover every selected server, then clean them up."""
    clients: list[McpStdioClient] = []
    tools: list[McpToolAdapter] = []
    names: list[str] = []
    try:
        for config in configs:
            # Authorization precedes any process or connection.
            await _authorize_server(handler, config)
            if config.transport != "stdio":
                raise NotImplementedError(
                    "Streamable HTTP MCP is not available: the official MCP Python "
                    "SDK could not be installed in this environment; stdio is supported."
                )
            if config.command is None:
                raise ValueError(f"MCP server '{config.name}' has no command")
            client = McpStdioClient(
                command=config.command,
                args=config.args,
                env_keys=config.env_keys,
                cwd=workspace,
                timeout=timeout,
            )
            await client.start()
            clients.append(client)
            names.append(config.name)
            for spec in await client.list_tools():
                tools.append(
                    McpToolAdapter(
                        client=client,
                        server=config.name,
                        spec=spec,
                        secret_values=secret_values,
                    )
                )
        yield McpRuntime(tools=tuple(tools), servers=tuple(names))
    finally:
        for client in reversed(clients):
            await client.close()
