"""Real Server-process acceptance for the Conversation/Run chat path."""

from __future__ import annotations

from pathlib import Path

import httpx

from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess, wait_for


def test_real_server_conversation_survives_across_runs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(base_url=server.url, headers=headers, trust_env=False) as api:
            assert api.get("/chat.html").status_code == 200
            conversation = api.post(
                "/api/runtime/conversations",
                json={"prompt": "hello", "source": "web"},
            ).json()
            conversation_id = conversation["conversation_id"]
            run = api.post(
                f"/api/runtime/conversations/{conversation_id}/messages",
                json={
                    "prompt": "first turn",
                    "source": "web",
                    "request_id": "real-process-1",
                },
            ).json()
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{run['run_id']}").json()["status"]
                    == "completed"
                ),
                timeout=20,
            )
            second = api.post(
                f"/api/runtime/conversations/{conversation_id}/messages",
                json={
                    "prompt": "second turn",
                    "source": "cli",
                    "request_id": "real-process-2",
                },
            ).json()
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{second['run_id']}").json()["status"]
                    == "completed"
                ),
                timeout=20,
            )
            detail = api.get(f"/api/runtime/conversations/{conversation_id}").json()
            assert len(detail["runs"]) == 2
            history = api.get(
                f"/api/runtime/conversations/{conversation_id}/history"
            ).json()
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "first turn",
                "second turn",
            ]
            assert api.get("/api/runtime/tasks").json() == []
    finally:
        server.stop()


def test_real_server_restart_keeps_conversation_and_completed_runs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(base_url=server.url, headers=headers, trust_env=False) as api:
            conversation = api.post(
                "/api/runtime/conversations",
                json={"prompt": "persist", "source": "cli"},
            ).json()
            conversation_id = conversation["conversation_id"]
            run = api.post(
                f"/api/runtime/conversations/{conversation_id}/messages",
                json={"prompt": "persist", "source": "cli", "request_id": "persist-1"},
            ).json()
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{run['run_id']}").json()["status"]
                    == "completed"
                ),
                timeout=20,
            )
        server.stop()
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            detail = api.get(f"/api/runtime/conversations/{conversation_id}").json()
            assert detail["conversation"]["conversation_id"] == conversation_id
            assert len(detail["runs"]) == 1
            assert detail["runs"][0]["status"] == "completed"
    finally:
        server.stop()
