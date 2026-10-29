"""Minimal MCP stdio client (JSON-RPC over newline-delimited stdio).

The official Python SDK could not be installed in this environment (its
dependency set -- httpx2/mcp-types/cryptography -- did not resolve within the
build budget), so this implements the documented stdio transport directly:
initialize, tools/list and tools/call. Authorization to *start* the process is
the caller's responsibility and must happen before ``start``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 10.0
MAX_LINE_BYTES = 1024 * 1024
_SAFE_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV")


class McpProtocolError(RuntimeError):
    pass


class McpTimeout(RuntimeError):
    pass


def build_mcp_env(env_keys: tuple[str, ...]) -> dict[str, str]:
    """Only a small safe baseline plus the names the config explicitly allows."""
    env = {name: os.environ[name] for name in _SAFE_ENV if name in os.environ}
    for name in env_keys:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...],
        env_keys: tuple[str, ...],
        cwd: Path,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.command = command
        self.args = args
        self.env = build_mcp_env(env_keys)
        self.cwd = cwd
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._stderr_tail = ""
        self._stderr_task: asyncio.Task[None] | None = None

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        total = 0
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            total += len(chunk)
            if total <= 8192:
                self._stderr_tail += chunk.decode("utf-8", "replace")

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "cairn", "version": "0.1"},
                },
            )
            await self._notify("notifications/initialized", {})
        except BaseException:
            # A failed or cancelled handshake must not leave the child alive.
            await self.close()
            raise

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        data = (json.dumps(message) + "\n").encode("utf-8")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        assert self._process is not None and self._process.stdout is not None
        request_id = self._next_id
        self._next_id += 1
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(
                self._await_response(request_id), self.timeout
            )
        except TimeoutError as exc:
            raise McpTimeout(f"MCP request '{method}' timed out") from exc

    async def _await_response(self, request_id: int) -> Any:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise McpProtocolError(
                    f"MCP server closed the stream: {self._stderr_tail.strip()}"
                )
            if len(line) > MAX_LINE_BYTES:
                raise McpProtocolError("MCP response exceeded the line limit")
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                # Notifications or unrelated responses do not answer this request.
                continue
            if "error" in message:
                error = message["error"]
                raise McpProtocolError(
                    error.get("message", "MCP error")
                    if isinstance(error, dict)
                    else str(error)
                )
            return message.get("result")

    async def list_tools(self) -> tuple[McpToolSpec, ...]:
        result = await self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        specs: list[McpToolSpec] = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            schema = tool.get("inputSchema")
            specs.append(
                McpToolSpec(
                    name=tool["name"],
                    description=str(tool.get("description", "")),
                    input_schema=schema
                    if isinstance(schema, dict)
                    else {"type": "object"},
                )
            )
        return tuple(specs)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if not isinstance(result, dict):
            raise McpProtocolError("MCP tools/call returned a non-object result")
        return result

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        self._process = None
