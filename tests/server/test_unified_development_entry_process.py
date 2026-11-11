"""Real Server process coverage for the unified Development entry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess


def test_explicit_development_survives_server_restart_and_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        client = RuntimeClient(server.url, token)

        async def create() -> dict[str, Any]:
            return await client.create_development(
                "Process-level explicit goal",
                "cli",
                "process-dev-1",
            )

        entry = asyncio.run(create())
        assert entry["task_id"]
        assert entry["run_id"]
        assert entry["conversation_id"]

        server.stop(crash=True)
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        client = RuntimeClient(server.url, token)

        async def after_restart() -> tuple[dict[str, Any], dict[str, Any]]:
            task = await client.request("GET", "tasks/" + entry["task_id"])
            retry = await client.create_development(
                "Process-level explicit goal",
                "cli",
                "process-dev-1",
            )
            return task, retry

        task, retry = asyncio.run(after_restart())
        assert task["task_id"] == entry["task_id"]
        assert retry["task_id"] == entry["task_id"]
        assert retry["run_id"] == entry["run_id"]
    finally:
        server.stop()
