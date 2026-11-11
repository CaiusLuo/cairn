"""Real PTY CLI relay over a Web-created Development Task."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.server_process import NativeClient, ServerProcess, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux Server/PTY acceptance"
)


def _client(server: ServerProcess) -> RuntimeClient:
    token = (server.state / "runtime.token").read_text().strip()
    return RuntimeClient(server.url, token)


def _wait_status(client: RuntimeClient, task_id: str, status: str) -> dict[str, Any]:
    def poll() -> dict[str, Any] | None:
        task = asyncio.run(client.inspect(task_id))
        return task if task["status"] == status else None

    return cast(dict[str, Any], wait_for(poll))


def test_web_created_task_continues_from_real_cli_and_survives_exit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    terminal: NativeClient | None = None
    try:
        server.start()
        client = _client(server)
        token_file = server.state / "runtime.token"
        entry = asyncio.run(
            client.create_development("Create from Web", "web", "pty-web-1")
        )
        task_id = entry["task_id"]
        created = _wait_status(client, task_id, "completed")

        terminal = NativeClient(source, server.url, token_file, task_id)
        wait_for(lambda: task_id.encode() in terminal.output)
        terminal.send("Continue through the real CLI")

        def first_cli_input() -> dict[str, Any] | None:
            task = asyncio.run(client.inspect(task_id))
            inputs = [
                item
                for item in task["inputs"]
                if item["source"] == "cli" and item["status"] == "completed"
            ]
            return task if inputs else None

        followed = wait_for(first_cli_input)
        assert followed["task_id"] == task_id
        assert (followed["session_id"], followed["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert len(followed["inputs"]) == 1
        assert followed["inputs"][0]["request_id"].startswith("cli:")
        # The receipt settles just before the CLI prints the final response;
        # wait for the client to return to its prompt before typing again.
        wait_for(lambda: "离线开发链路完成".encode() in terminal.output)
        time.sleep(0.3)

        # Exit the client while the next turn is executing. The Server owns it.
        (server.gates / "before").touch()
        terminal.send("Hold this turn open")
        wait_for(lambda: (server.gates / "before.entered").exists())
        assert asyncio.run(client.inspect(task_id))["status"] == "running"
        terminal.send("/exit")
        assert terminal.process.wait(timeout=10) == 0, bytes(terminal.output).decode(
            errors="replace"
        )[-2000:]
        assert asyncio.run(client.inspect(task_id))["status"] == "running"

        (server.gates / "before").unlink()
        finished = _wait_status(client, task_id, "completed")
        assert finished["task_id"] == task_id
        assert [item["request_id"] for item in finished["inputs"]] == [
            followed["inputs"][0]["request_id"],
            finished["inputs"][-1]["request_id"],
        ]
        assert finished["inputs"][-1]["status"] == "completed"
        assert finished["inputs"][-1]["request_id"].startswith("cli:")
    finally:
        if terminal is not None:
            terminal.close()
        server.stop()
