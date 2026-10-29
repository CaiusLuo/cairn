"""Declarative .cairn/mcp.toml parsing without credentials or silent fallback."""

from pathlib import Path

import pytest

from cairn.infra.mcp.config import load_mcp_servers


def _write(ws: Path, text: str) -> None:
    cfg = ws / ".cairn"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "mcp.toml").write_text(text, encoding="utf-8")


def test_missing_config_lists_nothing(tmp_path: Path) -> None:
    assert load_mcp_servers(tmp_path) == ()


def test_stdio_and_http_servers_parse(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[[servers]]\nname = "local"\ntransport = "stdio"\n'
        'command = "python"\nargs = ["-m", "demo"]\nenv_keys = ["DEMO_TOKEN"]\n'
        '\n[[servers]]\nname = "remote"\ntransport = "streamable_http"\n'
        'url = "https://example.test/mcp"\n',
    )
    servers = load_mcp_servers(tmp_path)
    assert [s.name for s in servers] == ["local", "remote"]
    assert servers[0].transport == "stdio"
    assert servers[0].args == ("-m", "demo")
    assert servers[0].env_keys == ("DEMO_TOKEN",)  # names only, no value
    assert servers[1].transport == "streamable_http"
    assert servers[1].url == "https://example.test/mcp"


def test_invalid_config_raises_not_falls_back(tmp_path: Path) -> None:
    _write(tmp_path, '[[servers]]\nname = "bad"\ntransport = "carrier-pigeon"\n')
    with pytest.raises(ValueError):
        load_mcp_servers(tmp_path)


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[[servers]]\nname = "dup"\ntransport = "stdio"\ncommand = "a"\n'
        '\n[[servers]]\nname = "dup"\ntransport = "stdio"\ncommand = "b"\n',
    )
    with pytest.raises(ValueError):
        load_mcp_servers(tmp_path)


def test_http_requires_url_and_stdio_requires_command(tmp_path: Path) -> None:
    _write(tmp_path, '[[servers]]\nname = "x"\ntransport = "stdio"\n')
    with pytest.raises(ValueError):
        load_mcp_servers(tmp_path)
