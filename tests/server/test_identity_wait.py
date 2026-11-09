"""Readiness retries happen in one client process and retain PID admission."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cairn.server import bootstrap as server
from cairn.server.client import RuntimeClient


@pytest.mark.parametrize("wrong_pid", [False, True])
def test_wait_retries_startup_transport_errors_but_rejects_wrong_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_pid: bool
) -> None:
    workspace = tmp_path / "source"
    workspace.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nworkspace = "{workspace}"\ndata_root = "{tmp_path / "state"}"\n'
        f'worktrees_root = "{tmp_path / "trees"}"\noffline = true\n'
    )
    monkeypatch.setattr(server, "read_token", lambda _: "isolated-token")
    calls = 0

    async def request(
        self: RuntimeClient,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1 and not wrong_pid:
            raise httpx.ConnectError("still starting")
        return {
            "pid": 999 if wrong_pid else 4321,
            "data_root": str(tmp_path / "state"),
            "workspace": str(workspace),
            "worktrees_root": str(tmp_path / "trees"),
            "url": "http://127.0.0.1:8777",
        }

    monkeypatch.setattr(RuntimeClient, "request", request)
    result = CliRunner().invoke(
        server.app,
        ["--config", str(config), "check-running", "--pid", "4321", "--wait", "2"],
    )
    assert (result.exit_code == 0) is not wrong_pid
    assert calls == (1 if wrong_pid else 2)
    if wrong_pid:
        assert isinstance(result.exception, RuntimeError)
        assert "PID does not match" in str(result.exception)
