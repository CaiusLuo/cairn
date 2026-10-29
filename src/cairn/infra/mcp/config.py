"""Parse ``.cairn/mcp.toml`` declaratively, without resolving credentials.

A server entry names its transport and, for ``env_keys``, only the *names* of
host environment variables it may receive; no secret value is ever stored in the
repository or returned from here. Invalid configuration raises rather than
falling back silently to "no servers".
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_RELATIVE = Path(".cairn") / "mcp.toml"
TRANSPORTS = frozenset({"stdio", "streamable_http"})


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()
    url: str | None = None


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _server(entry: object, index: int) -> McpServerConfig:
    if not isinstance(entry, dict):
        raise ValueError(f"servers[{index}] must be a table")
    name = entry.get("name")
    transport = entry.get("transport")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"servers[{index}] needs a non-empty name")
    if transport not in TRANSPORTS:
        raise ValueError(f"server '{name}' has unsupported transport {transport!r}")
    args = _string_list(entry.get("args"), f"server '{name}' args")
    env_keys = _string_list(entry.get("env_keys"), f"server '{name}' env_keys")
    command = entry.get("command")
    url = entry.get("url")
    if transport == "stdio":
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"stdio server '{name}' needs a command")
        if url is not None:
            raise ValueError(f"stdio server '{name}' must not set url")
    else:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"http server '{name}' needs an http(s) url")
        if command is not None:
            raise ValueError(f"http server '{name}' must not set command")
    return McpServerConfig(
        name=name.strip(),
        transport=transport,
        command=command.strip() if isinstance(command, str) else None,
        args=args,
        env_keys=env_keys,
        url=url,
    )


def parse_mcp_servers(values: dict[str, Any]) -> tuple[McpServerConfig, ...]:
    raw = values.get("servers", [])
    if not isinstance(raw, list):
        raise ValueError("servers must be an array of tables")
    servers = tuple(_server(entry, index) for index, entry in enumerate(raw))
    names = [server.name for server in servers]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ValueError(f"duplicate MCP server names: {sorted(duplicates)}")
    return servers


def load_mcp_servers(workspace_root: Path) -> tuple[McpServerConfig, ...]:
    path = workspace_root / CONFIG_RELATIVE
    try:
        path.lstat()
    except FileNotFoundError:
        return ()
    with path.open("rb") as handle:
        values = tomllib.load(handle)
    return parse_mcp_servers(values)
