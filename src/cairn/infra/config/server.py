"""Small TOML bootstrap configuration, shared by Server and remote-only CLI."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from cairn.core.budget import DEFAULT_MAX_STEPS, RunBudget
from cairn.infra.server_paths import ServerPaths, default_data_root

DEFAULT_CONFIG = Path("/etc/cairn/config.toml")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Services:
    wechat: bool = True
    schedule: bool = True
    memory: bool = True


@dataclass(frozen=True, slots=True)
class Logging:
    level: str = "info"
    access_log: bool = True


@dataclass(frozen=True, slots=True)
class ServerConfig:
    config_file: Path | None
    workspace: Path | None
    data_root: Path
    worktrees_root: Path
    host: str = "127.0.0.1"
    port: int = 8777
    web_root: Path | None = None
    provider_approval: Path | None = None
    offline: bool = False
    allow_shell: bool = False
    allow_network: bool = False
    budget: RunBudget = field(
        default_factory=lambda: RunBudget(
            DEFAULT_MAX_STEPS, max_seconds=900, max_stagnant_steps=12
        )
    )
    services: Services = field(default_factory=Services)
    logging: Logging = field(default_factory=Logging)

    @property
    def paths(self) -> ServerPaths:
        if self.workspace is None:
            raise ValueError("Set server.workspace, CAIRN_WORKSPACE or --workspace")
        return ServerPaths(self.workspace, self.data_root, self.worktrees_root)

    @property
    def url(self) -> str:
        host = "[::1]" if self.host == "::1" else self.host
        return f"http://{host}:{self.port}"


def _boolean(value: str) -> bool:
    if value.lower() not in {"true", "false", "1", "0", "on", "off"}:
        raise ValueError("Expected a boolean (true/false)")
    return value.lower() in {"true", "1", "on"}


def _path(value: Any, base: Path | None, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{name} must be a nonempty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if base is None:
            raise ValueError(f"{name} in CLI/environment must be absolute")
        path = base / path
    return path.resolve()


def load_server_config(
    path: Path | None = None, *, overrides: dict[str, Any] | None = None
) -> ServerConfig:
    """Explicit CLI > environment > TOML > defaults; no writes or secret loading.

    TOML paths are relative to that file. CLI/env paths must be absolute.
    The absent default file is optional; an explicitly selected file is required.
    """
    explicit = path is not None or "CAIRN_CONFIG" in os.environ
    chosen: Path | None = path or Path(
        os.environ.get("CAIRN_CONFIG", str(DEFAULT_CONFIG))
    )
    chosen = _path(chosen, None, "config")
    assert chosen is not None
    data: dict[str, Any] = {}
    if explicit or chosen.exists():
        if chosen.stat().st_size > 65536:
            raise ValueError("Server configuration exceeds 64 KiB")
        try:
            data = tomllib.loads(chosen.read_text())
        except tomllib.TOMLDecodeError:
            raise ValueError("Invalid Server TOML configuration") from None
    else:
        chosen = None
    sections = {"server", "budget", "services", "logging"}
    if set(data) - sections or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError("Expected only [server], [budget], [services], [logging]")
    server_fields = {
        "workspace",
        "data_root",
        "worktrees_root",
        "host",
        "port",
        "web_root",
        "provider_approval",
        "offline",
        "allow_shell",
        "allow_network",
    }
    budget_fields = {f.name for f in fields(RunBudget)}
    allowed = {
        "server": server_fields,
        "budget": budget_fields,
        "services": {f.name for f in fields(Services)},
        "logging": {f.name for f in fields(Logging)},
    }
    for section, names in allowed.items():
        unknown = set(data.get(section, {})) - names
        if unknown:
            raise ValueError(f"Unknown {section} keys: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {
        "data_root": default_data_root(),
        "workspace": None,
        "host": "127.0.0.1",
        "port": 8777,
        "offline": False,
        "allow_shell": False,
        "allow_network": False,
    }
    path_fields = {
        "workspace",
        "data_root",
        "worktrees_root",
        "web_root",
        "provider_approval",
    }
    for key, value in data.get("server", {}).items():
        values[key] = (
            _path(value, chosen.parent if chosen else None, key)
            if key in path_fields
            else value
        )
    bool_fields = {"offline", "allow_shell", "allow_network"}
    for key in server_fields:
        env = os.environ.get("CAIRN_" + key.upper())
        if env is not None:
            values[key] = (
                _path(env, None, key)
                if key in path_fields
                else _boolean(env)
                if key in bool_fields
                else int(env)
                if key == "port"
                else env
            )
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    if set(overrides) - (server_fields | budget_fields):
        raise ValueError("Unknown Server CLI override")
    for key in server_fields & overrides.keys():
        value = overrides[key]
        values[key] = _path(value, None, key) if key in path_fields else value
    values.setdefault("worktrees_root", values["data_root"] / "worktrees")
    for key in bool_fields:
        if type(values[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    if not isinstance(values["host"], str) or values["host"] not in LOOPBACK_HOSTS:
        raise ValueError(
            "Server host must be loopback; use an authenticated reverse proxy"
        )
    if type(values["port"]) is not int or not 1 <= values["port"] <= 65535:
        raise ValueError("Server port must be between 1 and 65535")
    budget: dict[str, Any] = {
        "max_steps": DEFAULT_MAX_STEPS,
        "max_seconds": 900.0,
        "max_stagnant_steps": 12,
    }
    budget.update(data.get("budget", {}))
    for key in budget_fields:
        env = os.environ.get("CAIRN_" + key.upper())
        if env is not None:
            budget[key] = (
                int(env)
                if key in {"max_steps", "max_tokens", "max_stagnant_steps"}
                else float(env)
            )
        if key in overrides:
            budget[key] = overrides[key]
    groups: dict[str, Any] = {}
    for section in ("services", "logging"):
        group = dict(data.get(section, {}))
        for key in allowed[section]:
            env = os.environ.get("CAIRN_" + section.upper() + "_" + key.upper())
            if env is not None:
                group[key] = env if key == "level" else _boolean(env)
        for key, value in group.items():
            if key != "level" and type(value) is not bool:
                raise ValueError(f"{section}.{key} must be boolean")
        groups[section] = (
            Services(**group) if section == "services" else Logging(**group)
        )
    if groups["logging"].level not in {
        "critical",
        "error",
        "warning",
        "info",
        "debug",
        "trace",
    }:
        raise ValueError("Invalid logging.level")
    return ServerConfig(
        config_file=chosen, **values, budget=RunBudget(**budget), **groups
    )
