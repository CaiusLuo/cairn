"""Public client admission and migration paths, without provider calls."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import cairn.terminal.client as client
import cairn.terminal.runtime_chat as chat
from cairn.infra.runtime_client import RuntimeClient, default_runtime_state_dir


@pytest.mark.parametrize("failure", ["offline", "token", "workspace"])
def test_connection_failure_never_enters_chat(
    tmp_path: Path, monkeypatch: Any, failure: str
) -> None:
    token = tmp_path / "runtime.token"
    token.write_text("private-test-token")
    token.chmod(0o600)

    def response(request: httpx.Request) -> httpx.Response:
        if failure == "offline":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            401 if failure == "token" else 200,
            json={"workspace": str(tmp_path / "other")},
        )

    transport = httpx.MockTransport(response)
    monkeypatch.setattr(
        client,
        "RuntimeClient",
        lambda *a, **kw: RuntimeClient(*a, transport=transport, **kw),
    )

    async def forbidden(*a: Any, **kw: Any) -> None:
        raise AssertionError("Rejected client entered chat")

    monkeypatch.setattr(chat, "main", forbidden)
    result = CliRunner().invoke(client.app, ["--runtime-token-file", str(token)])
    assert result.exit_code != 0
    assert "private-test-token" not in result.output
    assert {
        "offline": "Cannot connect",
        "token": "HTTP 401",
        "workspace": "does not match",
    }[failure] in result.output


def test_client_default_state_uses_xdg_and_needs_no_local_provider(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    token = default_runtime_state_dir() / "runtime.token"
    token.parent.mkdir(parents=True)
    token.write_text("test-token")
    token.chmod(0o600)
    seen = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"workspace": str(Path.cwd())})
    )
    monkeypatch.setattr(
        client,
        "RuntimeClient",
        lambda *a, **kw: RuntimeClient(*a, transport=transport, **kw),
    )

    async def remote(api: RuntimeClient, **kw: Any) -> None:
        seen.append(api.workspace)

    monkeypatch.setattr(chat, "main", remote)
    assert CliRunner().invoke(client.app, []).exit_code == 0
    assert seen == [Path.cwd()]


@pytest.mark.parametrize(
    "args",
    [
        ["session", "--help"],
        ["task", "run", "--help"],
        ["wechat", "serve", "--help"],
        ["runtime", "list", "--help"],
        ["local", "--help"],
    ],
)
def test_historical_commands_have_explicit_migration_paths(args: list[str]) -> None:
    result = CliRunner().invoke(client.app, args)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
