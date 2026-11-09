"""Conversation cancellation must preserve unknown tool effects."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.conversation.models import ConversationSource, RunStatus
from cairn.conversation.service import (
    ConversationRecoveryRequired,
    ConversationService,
)
from cairn.conversation.store import DomainStore


def test_cancel_during_tool_call_requires_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = DomainStore(tmp_path / "domain.sqlite3")
        conversation = store.create_conversation(
            workspace=workspace,
            source=ConversationSource.CLI,
            initial_prompt="tool",
        )

        class ToolExecution:
            async def __call__(
                self, task: Any, emit: Any, *, service: Any | None = None
            ) -> str:
                del emit
                assert service is not None
                service.record(task, "tool_call", {"tool": "write_file"})
                await asyncio.Event().wait()
                return "never"

        service = ConversationService(
            store,
            ToolExecution(),  # type: ignore[arg-type]
            workspace=workspace,
        )
        await service.start()
        try:
            run = service.submit(
                conversation.conversation_id,
                "write a file",
                ConversationSource.CLI,
                "tool-request",
            )
            for _ in range(100):
                if service.active_run_id == run.run_id:
                    break
                await asyncio.sleep(0.01)
            cancelled = await asyncio.wait_for(service.cancel(run.run_id), 5)
            assert cancelled.status is RunStatus.RECOVERY_REQUIRED
            assert cancelled.error == "cancelled_with_unknown_effect"
            with pytest.raises(ConversationRecoveryRequired):
                service.submit(
                    conversation.conversation_id,
                    "next",
                    ConversationSource.CLI,
                    "next-request",
                )
        finally:
            await service.close()

    asyncio.run(scenario())
