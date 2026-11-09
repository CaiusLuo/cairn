from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

import cairn.interfaces.cli.cli as legacy
import cairn.interfaces.cli.client as cli
import cairn.interfaces.cli.runtime_chat as shared
from cairn.server.client import RuntimeClient


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

    from cairn.interfaces.cli.frame import TranscriptView

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


def test_old_server_has_an_actionable_upgrade_message_without_local_fallback() -> None:
    import asyncio

    import pytest

    api = RuntimeClient(
        "http://127.0.0.1:8777",
        "private-test-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    with pytest.raises(ValueError, match="重启 cairn"):
        asyncio.run(shared.main(api))
