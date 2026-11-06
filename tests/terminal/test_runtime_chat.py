from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

import cairn.terminal.cli as legacy
import cairn.terminal.client as cli
import cairn.terminal.runtime_chat as shared
from cairn.infra.runtime_client import RuntimeClient


def test_interactive_option_dispatches_without_building_a_local_agent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    token = tmp_path / "runtime.token"
    token.write_text("test-token")
    token.chmod(0o600)
    seen = []

    async def local(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Shared CLI must not launch the old local main")

    async def remote(api: Any, **kwargs: Any) -> None:
        seen.append((api.url, kwargs))

    monkeypatch.setattr(legacy, "main", local)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"workspace": str(Path.cwd())})
    )
    monkeypatch.setattr(
        cli,
        "RuntimeClient",
        lambda *args, **kwargs: RuntimeClient(*args, transport=transport, **kwargs),
    )
    monkeypatch.setattr(shared, "main", remote)
    result = CliRunner().invoke(
        cli.app,
        [
            "--runtime-url",
            "http://127.0.0.1:8777",
            "--runtime-token-file",
            str(token),
            "--task-id",
            "task-test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [("http://127.0.0.1:8777", {"task_id": "task-test"})]


def test_terminal_eof_releases_a_waiting_reader(monkeypatch: Any) -> None:
    import asyncio
    from types import SimpleNamespace

    import pytest

    from cairn.terminal.frame import TranscriptView

    async def scenario() -> None:
        view = TranscriptView()

        async def eof() -> None:
            raise EOFError

        monkeypatch.setattr(
            view, "_app", SimpleNamespace(run_async=eof, is_running=False)
        )
        try:
            with pytest.raises(EOFError):
                await asyncio.wait_for(view.read(), 1)
        finally:
            await view.aclose()

    asyncio.run(scenario())
