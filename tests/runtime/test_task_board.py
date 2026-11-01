from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from cairn.core.task_board import TaskBoardStatus
from cairn.infra.task_board.store import (
    TaskBoardBusy,
    TaskBoardStore,
    TaskBoardValidationError,
)
from cairn.infra.tools.base import InvalidArguments
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_board.tools import (
    TaskCreateTool,
    TaskDetailTool,
    TaskTodoTool,
    TaskUpdateTool,
)


def test_invalid_update_does_not_corrupt_board(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "tasks")
    task = store.create(title="Valid")
    with pytest.raises(TaskBoardValidationError):
        store.update(task.task_id, title="")
    assert store.load()[0].title == "Valid"
    updated = store.update(task.task_id, status=TaskBoardStatus.COMPLETED)
    assert updated is not None and updated.status is TaskBoardStatus.COMPLETED


def test_create_invalid_issue_url_does_not_write(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "tasks")
    with pytest.raises(ValueError):
        store.create(title="Valid", issue_url="x" * 2001)
    assert store.load() == ()


def test_set_todo_done_invalid_value_does_not_corrupt(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "tasks")
    task = store.create(title="Valid")
    with_todo = store.add_todo(task.task_id, "item")
    assert with_todo is not None
    todo_id = with_todo.todos[0].todo_id
    with pytest.raises(TaskBoardValidationError):
        store.set_todo_done(task.task_id, todo_id, "invalid")  # type: ignore[arg-type]
    assert store.load()[0].todos[0].done is False


def test_task_tool_argument_contracts(tmp_path: Path) -> None:
    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    create = TaskCreateTool(service)
    update = TaskUpdateTool(service)
    todo = TaskTodoTool(service)

    for tool, bad in (
        (create, {"title": "x", "description": 123}),
        (create, {"title": "x", "extra": 1}),
        (update, {"task_id": "x", "status": "not-a-status"}),
        (update, {"task_id": "x"}),
        (todo, {"task_id": "x", "text": "   "}),
        (todo, {"task_id": "x", "text": "a", "todo_id": "b"}),
    ):
        with pytest.raises(InvalidArguments):
            tool.validate(bad)


def test_task_tools_return_failure_for_missing_task(tmp_path: Path) -> None:
    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    assert (
        asyncio.run(
            TaskUpdateTool(service).execute({"task_id": "missing", "progress": "x"})
        ).exit_code
        != 0
    )
    assert (
        asyncio.run(
            TaskTodoTool(service).execute({"task_id": "missing", "text": "x"})
        ).exit_code
        != 0
    )


def test_task_tool_closed_loop_create_detail_todo(tmp_path: Path) -> None:
    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    create = TaskCreateTool(service)
    detail = TaskDetailTool(service)
    update = TaskUpdateTool(service)
    todo = TaskTodoTool(service)

    async def flow() -> None:
        created = await create.execute(
            {"title": "Build panel", "description": "details"}
        )
        assert created.exit_code == 0
        task_id = created.stdout.split()[1].rstrip(":")
        shown = await detail.execute({"task_id": task_id})
        assert shown.exit_code == 0 and "Build panel" in shown.stdout
        added = await todo.execute({"task_id": task_id, "text": "write tests"})
        assert added.exit_code == 0 and "write tests" in added.stdout
        task = await service.aget(task_id)
        assert task is not None and task.todos
        todo_id = task.todos[0].todo_id
        done = await todo.execute(
            {"task_id": task_id, "todo_id": todo_id, "done": True}
        )
        assert done.exit_code == 0 and f"[x] {todo_id}" in done.stdout
        assert (
            await update.execute({"task_id": task_id, "status": "running"})
        ).exit_code == 0

    asyncio.run(flow())


def test_task_store_lock_does_not_block_event_loop(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "tasks", lock_timeout=0.2)
    service = TaskBoardService(store)
    ticks = 0

    def hold() -> None:
        with store._locked():
            time.sleep(0.6)

    async def scenario() -> None:
        nonlocal ticks
        holder = threading.Thread(target=hold)
        holder.start()
        await asyncio.sleep(0.05)
        beating = True

        async def heartbeat() -> None:
            nonlocal ticks
            while beating:
                ticks += 1
                await asyncio.sleep(0.02)

        task = asyncio.create_task(heartbeat())
        try:
            with pytest.raises(TaskBoardBusy):
                await service.alist()
        finally:
            beating = False
            await task
            holder.join(timeout=1)

    asyncio.run(scenario())
    assert ticks > 0


def test_task_controller_local_idea_and_ascii_todos(tmp_path: Path) -> None:
    from cairn.runtime.task_board.service import TaskBoardService
    from cairn.terminal.task_board.controller import TaskBoardController

    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    controller = TaskBoardController(service)

    async def flow() -> None:
        added = await controller.command("add Local idea")
        assert "已创建任务" in added.message
        task_id = service.current_task_id
        assert task_id is not None
        todo = await controller.command(f"todo {task_id} first step")
        assert "first step" in todo.message
        listed = await controller.command("list")
        assert task_id in listed.message
        shown = await controller.command(f"show {task_id}")
        assert "Title: Local idea" in shown.message

    asyncio.run(flow())


def test_controller_reports_busy_without_raising(tmp_path: Path) -> None:
    from cairn.runtime.task_board.service import TaskBoardService
    from cairn.terminal.task_board.controller import TaskBoardController

    store = TaskBoardStore(tmp_path / "tasks", lock_timeout=0.1)
    controller = TaskBoardController(TaskBoardService(store))

    def hold() -> None:
        with store._locked():
            time.sleep(0.3)

    async def flow() -> None:
        holder = threading.Thread(target=hold)
        holder.start()
        await asyncio.sleep(0.03)
        action = await controller.command("list")
        assert "暂忙" in action.message
        holder.join(timeout=1)

    asyncio.run(flow())


def test_controller_invalid_update_is_not_not_found(tmp_path: Path) -> None:
    from cairn.runtime.task_board.service import TaskBoardService
    from cairn.terminal.task_board.controller import TaskBoardController

    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    controller = TaskBoardController(service)
    task = service.create("Valid")

    async def flow() -> None:
        action = await controller.command(f"status {task.task_id} not-a-status")
        assert "未知状态" in action.message

    asyncio.run(flow())
