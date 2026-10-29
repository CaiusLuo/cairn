"""Parse ``.cairn/mcp.toml`` declaratively, without resolving credentials.

A server entry names its transport and, for ``env_keys``, only the *names* of
host environment variables it may receive; no secret value is ever stored in the
repository or returned from here. Invalid configuration raises rather than
falling back silently to "no servers".
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    header_env: tuple[tuple[str, str], ...] = ()


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(value)


def _server(entry: object, index: int) -> McpServerConfig:
    if not isinstance(entry, dict):
        raise ValueError(f"servers[{index}] must be a table")
    if entry.keys() - {
        "name",
        "transport",
        "command",
        "args",
        "env_keys",
        "url",
        "header_env",
    }:
        raise ValueError(f"servers[{index}] contains unknown fields")
    name = entry.get("name")
    transport = entry.get("transport")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"servers[{index}] needs a non-empty name")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) is None:
        raise ValueError(
            "MCP server names need 1-40 letters, digits, underscores or hyphens"
        )
    if not isinstance(transport, str) or transport not in TRANSPORTS:
        raise ValueError(f"server '{name}' has unsupported transport {transport!r}")
    args = _string_list(entry.get("args"), f"server '{name}' args")
    env_keys = _string_list(entry.get("env_keys"), f"server '{name}' env_keys")
    command = entry.get("command")
    url = entry.get("url")
    header_env = entry.get("header_env", {})
    if not isinstance(header_env, dict) or not all(
        isinstance(key, str)
        and re.fullmatch(r"[A-Za-z0-9-]+", key)
        and isinstance(value, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
        for key, value in header_env.items()
    ):
        raise ValueError(
            "header_env must map HTTP header names to environment variable names"
        )
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env_keys):
        raise ValueError("env_keys must contain environment variable names")
    if any("\0" in value for value in args):
        raise ValueError("MCP arguments must not contain NUL")
    if transport == "stdio":
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"stdio server '{name}' needs a command")
        if url is not None:
            raise ValueError(f"stdio server '{name}' must not set url")
        if header_env or "\0" in command:
            raise ValueError("stdio servers cannot set HTTP headers or contain NUL")
    else:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"http server '{name}' needs an http(s) url")
        if command is not None:
            raise ValueError(f"http server '{name}' must not set command")
        parsed_url = urlsplit(url)
        if (
            not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.fragment
            or parsed_url.query
            or args
            or env_keys
        ):
            raise ValueError(
                "HTTP MCP needs a plain endpoint URL; use header_env for credentials"
            )
    return McpServerConfig(
        name=name.strip(),
        transport=transport,
        command=command.strip() if isinstance(command, str) else None,
        args=args,
        env_keys=env_keys,
        url=url,
        header_env=tuple(header_env.items()),
    )


def parse_mcp_servers(values: dict[str, Any]) -> tuple[McpServerConfig, ...]:
    if values.keys() - {"servers"}:
        raise ValueError("MCP configuration contains unknown fields")
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
