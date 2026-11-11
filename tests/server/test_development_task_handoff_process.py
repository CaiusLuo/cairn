"""Real Server process coverage for cross-entry Development Task continuation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from cairn.interfaces.cli.runtime_chat import RemoteConversation
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess, wait_for


def _client(server: ServerProcess) -> RuntimeClient:
    token = (server.state / "runtime.token").read_text().strip()
    return RuntimeClient(server.url, token)


def _completed(client: RuntimeClient, task_id: str) -> dict[str, Any]:
    def poll() -> dict[str, Any] | None:
        task = asyncio.run(client.inspect(task_id))
        return task if task["status"] == "completed" else None

    return cast(dict[str, Any], wait_for(poll))


def test_web_task_continues_from_cli_across_server_restart(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        client = _client(server)
        entry = asyncio.run(
            client.create_development("Process Web task", "web", "process-web-1")
        )
        task_id = entry["task_id"]
        created = _completed(client, task_id)
        assert created["session_id"] and created["worktree"]

        async def append(prompt: str, request_id: str) -> dict[str, Any]:
            cli = RemoteConversation(client, mode="development")
            await cli.select(task_id)
            await cli.send(prompt, request_id=request_id)
            return await cli.wait()

        followed = asyncio.run(append("Continue from the CLI", "process-cli-follow-1"))
        assert followed["task_id"] == task_id
        assert (followed["session_id"], followed["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert followed["status"] == "completed"
        assert followed["inputs"][-1]["request_id"] == "process-cli-follow-1"
        assert followed["inputs"][-1]["status"] == "completed"

        duplicate = asyncio.run(append("Continue from the CLI", "process-cli-follow-1"))
        assert duplicate["attempts"] == followed["attempts"]
        assert duplicate["inputs"] == followed["inputs"]

        server.stop(crash=True)
        server.start()
        client = _client(server)
        restored = asyncio.run(client.inspect(task_id))
        assert restored["task_id"] == task_id
        assert (restored["session_id"], restored["worktree"], restored["status"]) == (
            created["session_id"],
            created["worktree"],
            "completed",
        )
        assert restored["inputs"] == followed["inputs"]

        replay = asyncio.run(
            client.message(
                task_id,
                "Continue from the CLI",
                source="cli",
                request_id="process-cli-follow-1",
                expected_sequence=created["sequence"],
            )
        )
        assert replay["attempts"] == followed["attempts"]
        assert replay["inputs"] == followed["inputs"]

        second = asyncio.run(
            append("Continue again after restart", "process-cli-follow-2")
        )
        assert second["task_id"] == task_id
        assert (second["session_id"], second["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert second["status"] == "completed"
        assert [item["request_id"] for item in second["inputs"]] == [
            "process-cli-follow-1",
            "process-cli-follow-2",
        ]
        assert asyncio.run(client.inspect(task_id))["inputs"] == second["inputs"]
    finally:
        server.stop()


def test_crashed_followup_requires_recovery_before_another_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        client = _client(server)
        entry = asyncio.run(
            client.create_development("Crash followup task", "web", "crash-web-1")
        )
        task_id = entry["task_id"]
        created = _completed(client, task_id)
        assert created["session_id"] and created["worktree"]

        (server.gates / "after").touch()
        accepted = asyncio.run(
            client.message(
                task_id,
                "Create another artifact",
                source="cli",
                request_id="crash-follow-1",
                expected_sequence=created["sequence"],
            )
        )
        assert accepted["status"] == "queued"
        wait_for(lambda: (server.gates / "after.entered").exists())
        server.stop(crash=True)
        (server.gates / "after").unlink()
        server.start()
        client = _client(server)

        restored = asyncio.run(client.inspect(task_id))
        assert restored["status"] == "recovery_required"
        assert (restored["session_id"], restored["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert restored["inputs"][-1]["request_id"] == "crash-follow-1"
        assert restored["inputs"][-1]["status"] == "recovery_required"
        assert restored["inputs"][-1]["diagnostic"] == "process_interrupted"

        with pytest.raises(httpx.HTTPStatusError) as blocked:
            asyncio.run(
                client.message(
                    task_id,
                    "Must be reviewed first",
                    source="web",
                    request_id="after-crash-1",
                    expected_sequence=restored["sequence"],
                )
            )
        assert blocked.value.response.status_code == 409
        assert blocked.value.response.json()["error"] == "task_recovery_required"

        report = asyncio.run(client.request("GET", f"tasks/{task_id}/recovery"))
        review = {
            key: report[key] for key in ("expected_sequence", "revision", "action")
        }
        review.update(
            reviewed_effects=True,
            note="Host reviewed the surviving artifact; do not replay the write",
        )
        paused = asyncio.run(
            client.request("POST", f"tasks/{task_id}/reconcile", review)
        )
        assert paused["status"] == "paused"
        reviewed_input = paused["inputs"][-1]
        assert reviewed_input["request_id"] == "crash-follow-1"
        assert reviewed_input["status"] == "paused"
        assert reviewed_input["settled_sequence"] == paused["sequence"]
        assert reviewed_input["diagnostic"] is None

        resumed = asyncio.run(
            client.message(
                task_id,
                "Continue after recovery review",
                source="cli",
                request_id="after-crash-2",
                expected_sequence=paused["sequence"],
            )
        )
        assert resumed["status"] == "queued"
        final = _completed(client, task_id)
        assert final["task_id"] == task_id
        assert (final["session_id"], final["worktree"]) == (
            created["session_id"],
            created["worktree"],
        )
        assert [item["request_id"] for item in final["inputs"]] == [
            "crash-follow-1",
            "after-crash-2",
        ]
        assert final["inputs"][-1]["status"] == "completed"
    finally:
        server.stop()
