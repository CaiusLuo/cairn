"""Activate configured MCP servers for one task or turn.

Starting a stdio server executes a host process and a streamable-http server
opens a network connection, so **authorization happens before the process or
connection exists**: MCP_SERVER plus LOCAL_SHELL (stdio) or NETWORK (http). Only
after that do we start the server and discover tools. The adapters are exposed
only for this task, and every call reports ``requires_authorization`` so the
runtime asks for MCP_TOOL instead of inheriting the baseline ALLOW.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry

from cairn.core.models import ToolCall, ToolResult
from cairn.core.permissions import (
    CapabilityRequest,
    PermissionCapability,
    PermissionHandler,
    PermissionRequest,
)
from cairn.infra.mcp.client import McpClient, McpToolSpec, open_mcp_client
from cairn.infra.mcp.config import McpServerConfig
from cairn.infra.tools.base import InvalidArguments
from cairn.runtime.lifecycle import await_owned, settle_owned


def _redact(text: str, secret_values: Sequence[str]) -> str:
    for value in secret_values:
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _redact_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            _redact(key, secrets): _redact_value(item, secrets)
            for key, item in value.items()
        }
    return value


class McpToolAdapter:
    """One server tool exposed to the model under a namespaced name."""

    requires_authorization = True

    def __init__(
        self,
        *,
        client: McpClient,
        server: str,
        spec: McpToolSpec,
        secret_values: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.server = server
        self.spec = spec
        self.secret_values = tuple(secret_values)
        raw_name = f"mcp__{server}__{spec.name}"
        self.name = raw_name
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw_name):
            digest = hashlib.sha256(raw_name.encode()).hexdigest()[:12]
            self.name = re.sub(r"[^A-Za-z0-9_-]", "_", raw_name)[:50] + "__" + digest
        self.description = _redact(
            spec.description or f"MCP tool {spec.name} on {server}", self.secret_values
        )
        schema = spec.input_schema or {"type": "object"}
        validator = validator_for(schema)
        validator.check_schema(schema)
        # An explicit empty registry rejects remote $refs without network access.
        self._validator = validator(schema, registry=Registry())

    def schema(self) -> dict[str, Any]:
        parameters = _redact_value(
            self.spec.input_schema or {"type": "object", "properties": {}},
            self.secret_values,
        )
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
        try:
            self._validator.validate(arguments)
        except ValidationError as exc:
            # Validator messages may include argument values (credentials).
            raise InvalidArguments(f"MCP arguments violate {exc.validator}") from None
        except Exception:
            raise InvalidArguments(
                "MCP input schema cannot be resolved locally"
            ) from None

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
        # Structured-only results must not silently become an empty success.
        if not parts and result.get("structuredContent") is not None:
            parts.append(
                json.dumps(
                    _redact_value(result["structuredContent"], self.secret_values),
                    ensure_ascii=False,
                )
            )
        if not parts and content:
            parts.append(
                "MCP returned non-text content; this terminal supports text results."
            )
        output = _redact("\n".join(parts), self.secret_values)
        if len(output) > 65536:
            output = output[:65536] + "\n[MCP output truncated]"
        is_error = bool(result.get("isError"))
        return ToolResult(
            stdout=output,
            stderr="MCP tool reported an error" if is_error else "",
            exit_code=1 if is_error else 0,
        )


class McpActivationError(RuntimeError):
    """Sanitized connection failure; raw SDK diagnostics may contain credentials."""


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
    # One owned task enters/exits the SDK's task-local AnyIO contexts. Tool
    # calls may use those clients from run_turn; cleanup stays in their owner.
    for config in configs:
        await _authorize_server(handler, config)
    ready: asyncio.Future[McpRuntime] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def own_connections() -> None:
        async with AsyncExitStack() as stack:
            adapters: list[McpToolAdapter] = []
            for config in configs:
                client = await stack.enter_async_context(
                    open_mcp_client(config, workspace=workspace, timeout=timeout)
                )
                for spec in await client.list_tools():
                    adapters.append(
                        McpToolAdapter(
                            client=client,
                            server=config.name,
                            spec=spec,
                            secret_values=(*secret_values, *client.secret_values),
                        )
                    )
            if len({tool.name for tool in adapters}) != len(adapters):
                raise ValueError("MCP discovery returned duplicate tool names")
            ready.set_result(
                McpRuntime(
                    tools=tuple(adapters),
                    servers=tuple(config.name for config in configs),
                )
            )
            await stop.wait()

    owner = asyncio.create_task(own_connections(), name="cairn-mcp-connections")
    primary: BaseException | None = None
    try:
        waitables: set[asyncio.Future[Any]] = {ready, owner}
        await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
        if owner.done():
            try:
                owner.result()
            except Exception as exc:
                raise McpActivationError(
                    f"MCP connection failed ({type(exc).__name__})"
                ) from None
        yield ready.result()
    except BaseException as exc:
        primary = exc
        raise
    finally:
        stop.set()
        if ready.done():
            cancellation = await await_owned(owner)
        else:
            outcome = await settle_owned(owner)
            cancellation = outcome.cancellation
            ready.cancel()
        if primary is None:
            if cancellation is not None:
                raise cancellation
            try:
                owner.result()
            except Exception as exc:
                raise McpActivationError(
                    f"MCP connection cleanup failed ({type(exc).__name__})"
                ) from None
