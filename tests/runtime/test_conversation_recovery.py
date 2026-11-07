"""Conversation Run restart and recovery behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.core.domain import ConversationSource, RunStatus
from cairn.infra.domain_store import DomainStore
from cairn.runtime.conversation_service import ConversationService


def test_startup_marks_running_recovery_and_schedules_queued(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = DomainStore(tmp_path / "domain.sqlite3")
        conversation = store.create_conversation(
            workspace=workspace,
            source=ConversationSource.CLI,
            initial_prompt="recover",
        )
        running = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.RUNNING,
            prompt="interrupted",
        )
        queued = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.QUEUED,
            prompt="queued",
        )

        class FakeExecution:
            async def __call__(
                self, task: Any, emit: Any, *, service: Any | None = None
            ) -> str:
                del task, emit, service
                return "ok"

        service = ConversationService(store, FakeExecution(), workspace=workspace)  # type: ignore[arg-type]
        await service.start()
        try:
            await asyncio.wait_for(service.queue.join(), 5)
        finally:
            await service.close()
        assert store.get_run(running.run_id).status is RunStatus.RECOVERY_REQUIRED  # type: ignore[union-attr]
        assert store.get_run(queued.run_id).status is RunStatus.COMPLETED  # type: ignore[union-attr]

    asyncio.run(scenario())
