"""Conversation Run restart and recovery behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.conversation.models import ConversationSource, RunStatus
from cairn.conversation.service import ConversationService
from cairn.conversation.store import DomainStore


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


def test_cancel_queued_run_before_worker_starts(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = DomainStore(tmp_path / "domain.sqlite3")
        conversation = store.create_conversation(
            workspace=workspace,
            source=ConversationSource.CLI,
            initial_prompt="cancel",
        )
        run = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.QUEUED,
            prompt="cancel me",
        )

        class FakeExecution:
            async def __call__(
                self, task: Any, emit: Any, *, service: Any | None = None
            ) -> str:
                del task, emit, service
                return "ok"

        service = ConversationService(store, FakeExecution(), workspace=workspace)  # type: ignore[arg-type]
        cancelled = await service.cancel(run.run_id)
        assert cancelled.status is RunStatus.CANCELLED
        await service.start()
        await service.close()
        assert store.get_run(run.run_id).status is RunStatus.CANCELLED  # type: ignore[union-attr]

    asyncio.run(scenario())
