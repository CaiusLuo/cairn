"""Legacy development execution projected into DomainStore and TaskBoard."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cairn.core.domain import ConversationSource, TaskStatus
from cairn.infra.domain_store import DomainStore
from cairn.infra.runtime_catalog import RuntimeCatalogue, RuntimeTask
from cairn.infra.task_board.store import TaskBoardStore
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService


async def _executor(_task: RuntimeTask, _emit: object) -> str:
    return "done"


async def _service(tmp_path: Path) -> tuple[TaskService, DomainStore, TaskBoardStore]:
    source = tmp_path / "source"
    source.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    catalogue = RuntimeCatalogue(
        tmp_path / "catalogue", forbidden_roots=(source, worktrees)
    )
    board_store = TaskBoardStore(tmp_path / "board")
    domain = DomainStore(tmp_path / "domain.sqlite3")
    service = TaskService(
        catalogue,
        _executor,
        board=TaskBoardService(board_store, project=str(source)),
    )
    service.domain_store = domain
    await service.start()
    return service, domain, board_store


def _task(task_id: str = "task-projected") -> RuntimeTask:
    return RuntimeTask(
        task_id=task_id,
        prompt="build the thing",
        source="wechat",
        mode="development",
        delivery_recipient="owner",
        delivery_account="bot-1",
    )


async def _record(
    service: TaskService, task: RuntimeTask, status: str, **updates: object
) -> None:
    current = service.inspect(task.task_id)
    service.record(
        current.model_copy(update={"status": status, **updates}), "test_" + status
    )
    await asyncio.sleep(0.05)


def test_development_task_projection_statuses_order_and_worktree(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service, domain, _board = await _service(tmp_path)
        try:
            conversation = domain.create_conversation(
                workspace=tmp_path,
                source=ConversationSource.CLI,
                initial_prompt="goal",
                request_id="projection-conv",
            )
            domain.create_task(
                conversation_id=conversation.conversation_id,
                goal="build the thing",
                title="User title",
                task_id="task-projected",
                metadata={"user_key": "keep-me"},
            )
            task = _task()
            service.record(task, "accepted")
            await asyncio.sleep(0.05)
            queued = domain.get_task("task-projected")
            assert queued is not None
            assert queued.status == TaskStatus.TODO

            await _record(service, task, "running")
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.status == TaskStatus.IN_PROGRESS
            assert current.title == "User title"
            assert current.metadata["user_key"] == "keep-me"
            assert current.metadata["execution_status"] == "running"

            worktree = str(tmp_path / "worktrees" / "one")
            await _record(
                service,
                task,
                "completed",
                result="result",
                worktree=worktree,
                branch="cairn/dev",
            )
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.worktree == worktree
            assert current.branch == "cairn/dev"
            assert current.metadata["execution_status"] == "completed"
            # Execution completion does not silently mark the business task DONE.
            assert current.status == TaskStatus.IN_PROGRESS

            await _record(service, task, "paused", diagnostic="budget")
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.status == TaskStatus.WAITING
            assert current.metadata["execution_diagnostic"] == "budget"

            await _record(service, task, "failed", diagnostic="boom")
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.status == TaskStatus.WAITING
            assert current.metadata["execution_status"] == "failed"

            # Out-of-order projection must not replace newer execution facts.
            stale = task.model_copy(update={"status": "running", "sequence": 1})
            service._project_domain(stale)
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.metadata["execution_status"] == "failed"

            # Explicit DONE is never reopened by execution projection.
            domain.update_task("task-projected", status=TaskStatus.DONE)
            service._project_domain(
                service.inspect("task-projected").model_copy(
                    update={"status": "failed", "diagnostic": "late"}
                )
            )
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.status == TaskStatus.DONE
        finally:
            await service.close()

    asyncio.run(scenario())


def test_legacy_only_task_is_not_created_in_domain_store(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, domain, _board = await _service(tmp_path)
        try:
            task = _task("task-legacy-only")
            service.record(task, "accepted")
            await asyncio.sleep(0.05)
            assert domain.get_task("task-legacy-only") is None
        finally:
            await service.close()

    asyncio.run(scenario())


def test_domain_projection_failure_is_recorded_without_losing_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service, domain, _board = await _service(tmp_path)
        try:
            conversation = domain.create_conversation(
                workspace=tmp_path,
                source=ConversationSource.CLI,
                initial_prompt="goal",
            )
            domain.create_task(
                conversation_id=conversation.conversation_id,
                goal="goal",
                task_id="task-projected",
            )
            task = _task()
            service.record(task, "accepted")
            await asyncio.sleep(0.05)

            original = domain.project_task_execution
            domain.project_task_execution = (  # type: ignore[method-assign]
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    OSError("domain write failed")
                )
            )
            service.projections["task-projected"] = service.inspect("task-projected")
            service.projection_signal.set()
            await asyncio.sleep(0.05)
            assert service.domain_projection_error == "OSError"
            # RuntimeCatalogue remains the execution authority.
            assert service.inspect("task-projected").status == "queued"
            domain.project_task_execution = original  # type: ignore[method-assign]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_projection_preserves_concurrent_user_closed_and_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service, domain, _board = await _service(tmp_path)
        try:
            conversation = domain.create_conversation(
                workspace=tmp_path,
                source=ConversationSource.CLI,
                initial_prompt="goal",
            )
            domain.create_task(
                conversation_id=conversation.conversation_id,
                goal="goal",
                title="User title",
                task_id="task-projected",
                metadata={"user_key": "keep-me"},
            )
            task = _task()
            service.record(task, "accepted")
            await asyncio.sleep(0.05)
            domain.update_task(
                "task-projected",
                status=TaskStatus.CLOSED,
                metadata={"user_key": "still-here", "user_note": "do not overwrite"},
            )
            service._project_domain(
                service.inspect("task-projected").model_copy(
                    update={"status": "completed", "result": "late result"}
                )
            )
            current = domain.get_task("task-projected")
            assert current is not None
            assert current.status == TaskStatus.CLOSED
            assert current.title == "User title"
            assert current.metadata["user_key"] == "still-here"
            assert current.metadata["user_note"] == "do not overwrite"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_concurrent_user_close_and_projection_keep_closed(tmp_path: Path) -> None:
    domain = DomainStore(tmp_path / "domain.sqlite3")
    conversation = domain.create_conversation(
        workspace=tmp_path,
        source=ConversationSource.CLI,
        initial_prompt="goal",
    )
    domain.create_task(
        conversation_id=conversation.conversation_id,
        goal="goal",
        task_id="task-race",
        status=TaskStatus.IN_PROGRESS,
    )

    def project() -> None:
        domain.project_task_execution(
            "task-race",
            execution_metadata={
                "execution_status": "completed",
                "execution_task_id": "task-race",
                "execution_sequence": 4,
            },
            status=TaskStatus.IN_PROGRESS,
        )

    def close() -> None:
        domain.update_task("task-race", status=TaskStatus.CLOSED)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(project), executor.submit(close)]
        for future in futures:
            future.result()

    final = domain.get_task("task-race")
    assert final is not None
    assert final.status == TaskStatus.CLOSED
