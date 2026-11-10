"""Durable Run/Session identity binding and missing-Session recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cairn.conversation.models import Conversation, ConversationSource, RunStatus
from cairn.conversation.service import ConversationService
from cairn.conversation.store import DomainStore


def _conversation(store: DomainStore, workspace: Path) -> Conversation:
    return store.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="session binding",
    )


def test_bind_run_session_is_idempotent_and_immutable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DomainStore(tmp_path / "domain.sqlite3")
    conversation = _conversation(store, workspace)
    run = store.create_run(
        conversation_id=conversation.conversation_id,
        status=RunStatus.QUEUED,
        prompt="bind",
        request_id="bind-request",
    )
    session_id = str(uuid4())

    bound = store.bind_run_session(run.run_id, session_id)
    assert bound.session_id == session_id
    assert store.get_run(run.run_id).session_id == session_id  # type: ignore[union-attr]

    repeated = store.bind_run_session(run.run_id, session_id)
    assert repeated.session_id == session_id
    with pytest.raises(ValueError, match="another Session"):
        store.bind_run_session(run.run_id, str(uuid4()))


def test_run_persists_session_before_execution_finishes(tmp_path: Path) -> None:
    class BlockingExecution:
        sensitive_values: tuple[str, ...] = ()

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.session_id: str | None = None

        async def __call__(
            self, task: Any, emit: Any, *, service: Any | None = None
        ) -> str:
            del emit
            assert service is not None
            self.session_id = str(uuid4())
            updated = task.model_copy(update={"session_id": self.session_id})
            service.task = updated
            service.record(updated, "conversation_ready")
            service.bind_session(self.session_id)
            self.started.set()
            await self.release.wait()
            return "done"

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = DomainStore(tmp_path / "domain.sqlite3")
        execution = BlockingExecution()
        service = ConversationService(store, execution, workspace=workspace)  # type: ignore[arg-type]
        await service.start()
        try:
            conversation = _conversation(store, workspace)
            run = service.submit(
                conversation.conversation_id,
                "slow",
                ConversationSource.CLI,
                "slow-request",
            )
            await asyncio.wait_for(execution.started.wait(), 5)
            current = store.get_run(run.run_id)
            assert current is not None
            assert current.status is RunStatus.RUNNING
            assert current.session_id == execution.session_id
            execution.release.set()
            await asyncio.wait_for(service.queue.join(), 5)
            finished = store.get_run(run.run_id)
            assert finished is not None
            assert finished.status is RunStatus.COMPLETED
            assert finished.session_id == execution.session_id
        finally:
            await service.close()

    asyncio.run(scenario())


def test_missing_session_recovery_requires_explicit_auditable_abandon(
    tmp_path: Path,
) -> None:
    class UnusedExecution:
        sensitive_values: tuple[str, ...] = ()

        async def __call__(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("missing-Session recovery must not execute")

    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = DomainStore(tmp_path / "domain.sqlite3")
        conversation = _conversation(store, workspace)
        run = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.RECOVERY_REQUIRED,
            prompt="interrupted",
            request_id="interrupted-request",
            error="process_interrupted",
        )
        service = ConversationService(
            store,
            UnusedExecution(),  # type: ignore[arg-type]
            workspace=workspace,
        )
        report = service.recovery(run.run_id)
        assert report["missing_session"] is True
        assert report["session_id"] is None
        assert report["action"] == "abandon_without_replay"
        assert report["diagnostic"] == "process_interrupted"

        with pytest.raises(ValueError, match="abandon_without_replay"):
            service.reconcile(
                run.run_id,
                {
                    "action": "acknowledge_checkpoint",
                    "reviewed_effects": True,
                    "note": "looked",
                },
            )
        with pytest.raises(ValueError, match="without secrets"):
            service.reconcile(
                run.run_id,
                {
                    "action": "abandon_without_replay",
                    "reviewed_effects": True,
                    "note": "   ",
                },
            )

        reconciled = service.reconcile(
            run.run_id,
            {
                "action": "abandon_without_replay",
                "reviewed_effects": True,
                "note": "No Session was created; no side effects observed.",
            },
        )
        assert reconciled.status is RunStatus.CANCELLED
        assert reconciled.error == "recovery_abandoned_without_session"
        assert reconciled.metadata["recovery_action"] == "abandon_without_replay"
        assert reconciled.metadata["recovery_diagnostic"] == "process_interrupted"
        assert (
            reconciled.metadata["recovery_note"]
            == "No Session was created; no side effects observed."
        )

    asyncio.run(scenario())
