"""Process-level restart coverage for a persisted Development Handoff intent."""

from __future__ import annotations

from pathlib import Path

import httpx

from cairn.conversation.models import ConversationSource, RunStatus
from cairn.conversation.store import DomainStore
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess, wait_for


def test_restart_resumes_persisted_handoff_intent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        store = DomainStore(server.state / "domain.sqlite3")
        conversation = store.create_conversation(
            workspace=source,
            source=ConversationSource.CLI,
            initial_prompt="handoff restart",
        )
        run = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.RUNNING,
            prompt="handoff restart",
            request_id="handoff-restart",
            source=ConversationSource.CLI,
        )
        store.begin_handoff(
            run_id=run.run_id,
            conversation_id=conversation.conversation_id,
            request_id="handoff:" + run.run_id,
            prompt="handoff restart",
            source="cli",
        )
        server.stop(crash=True)
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(
            base_url=server.url, headers=headers, trust_env=False, timeout=10
        ) as api:
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/handoffs/{run.run_id}").status_code == 200
                ),
                timeout=10,
            )
            report = (
                api.get(f"/api/runtime/handoffs/{run.run_id}").raise_for_status().json()
            )
            assert report["handoff"]["status"] == "completed", report
            assert report["handoff"]["runtime_task_id"] is not None
            assert report["handoff"]["domain_task_id"] is not None
            current = (
                api.get(f"/api/runtime/runs/{run.run_id}").raise_for_status().json()
            )
            assert current["status"] == "completed", current
            assert current["task_id"] == report["handoff"]["domain_task_id"]
    finally:
        server.stop()
