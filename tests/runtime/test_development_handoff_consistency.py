"""Durable Development Handoff identity and recovery tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from cairn.conversation.models import ConversationSource, Run, RunStatus
from cairn.conversation.service import ConversationService
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeCatalogue, RuntimeTask
from cairn.development.handoff import DevelopmentHandoff
from cairn.development.models import HandoffStatus, TaskStatus
from cairn.development.service import TaskService


class _NoopExecution:
    sensitive_values: tuple[str, ...] = ()

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("handoff test should not execute the agent")


def _setup(tmp_path: Path) -> tuple[Path, DomainStore, TaskService, DevelopmentHandoff]:
    source = tmp_path / "source"
    source.mkdir()
    store = DomainStore(tmp_path / "domain.sqlite3")
    catalogue = RuntimeCatalogue(
        tmp_path / "runtime",
        forbidden_roots=(source, tmp_path / "worktrees"),
    )
    tasks = TaskService(catalogue, _NoopExecution())
    tasks.worker = asyncio.create_task(asyncio.sleep(3600))
    handoff = DevelopmentHandoff(store, _NoopExecution(), tasks)  # type: ignore[arg-type]
    return source, store, tasks, handoff


def _conversation_run(
    store: DomainStore, source: Path, *, request_id: str = "handoff-run"
) -> Run:
    conversation = store.create_conversation(
        workspace=source,
        source=ConversationSource.CLI,
        initial_prompt="goal",
    )
    return store.create_run(
        conversation_id=conversation.conversation_id,
        status=RunStatus.RUNNING,
        prompt="go create",
        request_id=request_id,
        source=ConversationSource.CLI,
    )


def _execution(prompt: str = "go create") -> RuntimeTask:
    return RuntimeTask(prompt=prompt, source="cli", mode="development")


def _evidence() -> dict[str, Any]:
    return {"budget": {}}


async def _cleanup(tasks: TaskService) -> None:
    worker = tasks.worker
    assert worker is not None
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    tasks.catalogue.close()


def test_first_handoff_creates_one_linked_task_and_completes_intent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            handoff.complete(run, _execution(), _evidence)
            record = store.get_handoff(run.run_id)
            assert record is not None
            assert record.status is HandoffStatus.COMPLETED
            assert record.runtime_task_id
            assert record.domain_task_id
            stored_run = store.get_run(run.run_id)
            assert stored_run is not None
            assert stored_run.status is RunStatus.COMPLETED
            assert stored_run.task_id == record.domain_task_id
            domain_task = store.get_task(record.domain_task_id)
            assert domain_task is not None
            assert domain_task.conversation_id == run.conversation_id
            assert domain_task.metadata["handoff_run_id"] == run.run_id
            assert (
                tasks.inspect(record.runtime_task_id).task_id == record.domain_task_id
            )
            assert len(store.list_tasks()) == 1
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_duplicate_handoff_for_same_run_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            handoff.complete(run, _execution(), _evidence)
            first = store.get_handoff(run.run_id)
            assert first is not None
            handoff.complete(run, _execution(), _evidence)
            second = store.get_handoff(run.run_id)
            assert second is not None
            assert second.runtime_task_id == first.runtime_task_id
            assert second.domain_task_id == first.domain_task_id
            assert len(store.list_tasks()) == 1
            assert len(tasks.list_tasks()) == 1
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_two_different_runs_in_one_conversation_create_two_tasks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        conversation = store.create_conversation(
            workspace=source,
            source=ConversationSource.CLI,
            initial_prompt="two goals",
        )
        first_run = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.RUNNING,
            prompt="first goal",
            request_id="first-handoff",
            source=ConversationSource.CLI,
        )
        second_run = store.create_run(
            conversation_id=conversation.conversation_id,
            status=RunStatus.RUNNING,
            prompt="second goal",
            request_id="second-handoff",
            source=ConversationSource.CLI,
        )
        try:
            handoff.complete(first_run, _execution("first goal"), _evidence)
            handoff.complete(second_run, _execution("second goal"), _evidence)
            first = store.get_handoff(first_run.run_id)
            second = store.get_handoff(second_run.run_id)
            assert first is not None and second is not None
            assert first.domain_task_id != second.domain_task_id
            assert first.runtime_task_id != second.runtime_task_id
            assert len(store.list_tasks()) == 2
            assert len(tasks.list_tasks()) == 2
            first_stored = store.get_run(first_run.run_id)
            second_stored = store.get_run(second_run.run_id)
            assert first_stored is not None and second_stored is not None
            assert first_stored.task_id == first.domain_task_id
            assert second_stored.task_id == second.domain_task_id
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_runtime_task_created_then_crash_reuses_task_on_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            record = store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="go create",
                source="cli",
            )
            task = tasks.submit(
                "go create",
                "cli",
                request_id=record.request_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
            # Simulate a crash before the handoff cursor was updated.
            recovered = handoff.recover_incomplete()
            assert len(recovered) == 1
            final = store.get_handoff(run.run_id)
            assert final is not None
            assert final.status is HandoffStatus.COMPLETED
            assert final.runtime_task_id == task.task_id
            assert final.domain_task_id == task.task_id
            assert len(tasks.list_tasks()) == 1
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_domain_task_created_then_crash_fills_run_link_on_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            record = store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="go create",
                source="cli",
            )
            task = tasks.submit(
                "go create",
                "cli",
                request_id=record.request_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.RUNTIME_CREATED,
                runtime_task_id=task.task_id,
            )
            domain_task = store.create_task_if_absent(
                conversation_id=run.conversation_id,
                task_id=task.task_id,
                title="go create",
                goal="go create",
                status=TaskStatus.IN_PROGRESS,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.DOMAIN_CREATED,
                domain_task_id=domain_task.task_id,
            )
            recovered = handoff.recover_incomplete()
            assert len(recovered) == 1
            stored = store.get_run(run.run_id)
            assert stored is not None
            assert stored.task_id == domain_task.task_id
            final = store.get_handoff(run.run_id)
            assert final is not None
            assert final.status is HandoffStatus.COMPLETED
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_failed_handoff_retries_with_stable_request_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            # Simulate a stopped Runtime before the handoff is admitted.
            worker = tasks.worker
            assert worker is not None
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            with pytest.raises(RuntimeError):
                handoff.complete(run, _execution(), _evidence)
            pending = store.get_handoff(run.run_id)
            assert pending is not None
            assert pending.status is HandoffStatus.INTENT
            assert pending.diagnostic == "RuntimeError"
            assert store.list_tasks() == ()

            tasks.worker = asyncio.create_task(asyncio.sleep(3600))
            recovered = handoff.recover_incomplete()
            assert len(recovered) == 1
            final = store.get_handoff(run.run_id)
            assert final is not None
            assert final.status is HandoffStatus.COMPLETED
            assert len(tasks.list_tasks()) == 1
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())


def test_conversation_start_resumes_incomplete_handoff_before_scan(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks, _handoff = _setup(tmp_path)
        run = _conversation_run(store, source)
        try:
            record = store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="go create",
                source="cli",
            )
            task = tasks.submit(
                "go create",
                "cli",
                request_id=record.request_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.RUNTIME_CREATED,
                runtime_task_id=task.task_id,
            )
            service = ConversationService(
                store,
                _NoopExecution(),  # type: ignore[arg-type]
                workspace=source,
                legacy_service=tasks,
            )
            await service.start()
            try:
                final = store.get_handoff(run.run_id)
                assert final is not None
                assert final.status is HandoffStatus.COMPLETED
                stored = store.get_run(run.run_id)
                assert stored is not None
                assert stored.task_id == task.task_id
            finally:
                await service.close()
        finally:
            await _cleanup(tasks)

    asyncio.run(scenario())
