"""Real PTY CLI routing between Tasks, Conversations and Runs."""

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


def _runs(client: RuntimeClient, conversation_id: str) -> list[dict[str, Any]]:
    detail = asyncio.run(client.get_conversation(conversation_id))
    return list(detail.get("runs", []))


def _wait_runs(
    client: RuntimeClient, conversation_id: str, count: int
) -> list[dict[str, Any]]:
    def poll() -> list[dict[str, Any]] | None:
        rows = _runs(client, conversation_id)
        return (
            rows if len(rows) == count and rows[-1]["status"] == "completed" else None
        )

    return cast(list[dict[str, Any]], wait_for(poll))


def _wait_cli_inputs(client: RuntimeClient, task_id: str, count: int) -> dict[str, Any]:
    def poll() -> dict[str, Any] | None:
        task = asyncio.run(client.inspect(task_id))
        settled = [
            item
            for item in task["inputs"]
            if item["source"] == "cli" and item["status"] == "completed"
        ]
        return task if len(settled) >= count else None

    return cast(dict[str, Any], wait_for(poll))


def _wait_quiet(terminal: NativeClient, timeout: float = 5) -> None:
    """Wait for the CLI to finish printing before typing the next line."""
    deadline = time.monotonic() + timeout
    size = -1
    while time.monotonic() < deadline:
        current = len(terminal.output)
        if current == size:
            return
        size = current
        time.sleep(0.2)
    raise AssertionError("Terminal output did not settle")


def test_use_and_task_use_route_to_the_right_client(tmp_path: Path) -> None:
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
            client.create_development("Routing task", "web", "routing-web-1")
        )
        task_id = entry["task_id"]
        created = _wait_status(client, task_id, "completed")
        assert created["session_id"] and created["worktree"]

        conversation = asyncio.run(
            client.create_conversation(
                "Conversation seed", source="web", request_id="routing-conv-1"
            )
        )
        conversation_id = conversation["conversation_id"]
        seed = asyncio.run(
            client.send_conversation(
                conversation_id,
                "Conversation seed",
                source="web",
                request_id="routing-run-1",
            )
        )
        run_id = seed["run_id"]
        _wait_runs(client, conversation_id, 1)

        terminal = NativeClient(source, server.url, token_file)
        wait_for(lambda: b"Shared Runtime" in terminal.output)

        # The client starts as RemoteChat; /task use must switch it to the
        # RemoteConversation for the explicit Task.
        terminal.send(f"/task use {task_id}")
        wait_for(lambda: task_id.encode() in terminal.output)
        terminal.send("Via task use")
        first = _wait_cli_inputs(client, task_id, 1)
        assert first["task_id"] == task_id
        assert (first["session_id"], first["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert len(_runs(client, conversation_id)) == 1
        _wait_quiet(terminal)

        # Switching to a Run must replace the Task client with RemoteChat.
        terminal.send(f"/use {run_id}")
        wait_for(lambda: conversation_id.encode() in terminal.output)
        _wait_quiet(terminal)
        terminal.send("Via run switch")
        runs = _wait_runs(client, conversation_id, 2)
        assert runs[-1]["conversation_id"] == conversation_id
        assert len(asyncio.run(client.inspect(task_id))["inputs"]) == 1
        _wait_quiet(terminal)

        # Switching to a Conversation keeps RemoteChat and appends another Run.
        terminal.send(f"/use {conversation_id}")
        _wait_quiet(terminal)
        terminal.send("Via conversation switch")
        runs = _wait_runs(client, conversation_id, 3)
        assert runs[-1]["conversation_id"] == conversation_id
        _wait_quiet(terminal)

        # Switching back to the Task must reuse the original identity.
        terminal.send(f"/use {task_id}")
        _wait_quiet(terminal)
        terminal.send("Via use task")
        second = _wait_cli_inputs(client, task_id, 2)
        assert second["task_id"] == task_id
        assert (second["session_id"], second["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert [item["source"] for item in second["inputs"]] == ["cli", "cli"]
        _wait_quiet(terminal)

        terminal.send("/exit")
        assert terminal.process.wait(timeout=10) == 0, bytes(terminal.output).decode(
            errors="replace"
        )[-2000:]
        assert len(_runs(client, conversation_id)) == 3
    finally:
        if terminal is not None:
            terminal.close()
        server.stop()
