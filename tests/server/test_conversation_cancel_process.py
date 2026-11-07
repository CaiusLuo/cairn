"""Real Server HTTP acceptance for Conversation cancel/recovery protocols."""

from __future__ import annotations

from pathlib import Path

import httpx

from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess, wait_for


def test_real_http_cancel_is_post_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    (server.gates / "before").touch()
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(base_url=server.url, headers=headers, trust_env=False) as api:
            conversation = api.post(
                "/api/runtime/conversations",
                json={"prompt": "cancel", "source": "web"},
            ).json()
            conversation_id = conversation["conversation_id"]
            run = api.post(
                f"/api/runtime/conversations/{conversation_id}/messages",
                json={
                    "prompt": "cancel me",
                    "source": "web",
                    "request_id": "real-cancel",
                },
            ).json()
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{run['run_id']}").json()["status"]
                    == "running"
                ),
                timeout=20,
            )
            before = api.get(f"/api/runtime/runs/{run['run_id']}").json()
            response = api.get(f"/api/runtime/runs/{run['run_id']}/cancel")
            assert response.status_code == 405
            after = api.get(f"/api/runtime/runs/{run['run_id']}").json()
            assert after["status"] == before["status"]
            response = api.post(f"/api/runtime/runs/{run['run_id']}/cancel")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "cancelled"
    finally:
        server.stop()
