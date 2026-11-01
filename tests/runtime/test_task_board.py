from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cairn.core.task_board import TaskBoardItem, TaskBoardStatus
from cairn.infra.task_board.store import (
    TaskBoardBusy,
    TaskBoardStore,
    TaskBoardValidationError,
)
from cairn.infra.tools.base import InvalidArguments, Tool
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


def test_task_bar_reads_fresh_shared_state_across_services(tmp_path: Path) -> None:
    from cairn.runtime.task_board.service import TaskBoardService
    from cairn.terminal.task_board.controller import TaskBoardController

    store_a = TaskBoardStore(tmp_path / "tasks")
    service_a = TaskBoardService(store_a)
    service_b = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    controller_a = TaskBoardController(service_a)

    task = service_a.create("shared task")
    service_a.current_task_id = task.task_id
    service_b.add_todo(task.task_id, "from B")

    async def flow() -> None:
        lines = await controller_a.render_lines()
        assert any("from B" in line for line in lines)

    asyncio.run(flow())


def test_task_todo_repeated_done_is_success(tmp_path: Path) -> None:
    from cairn.runtime.task_board.service import TaskBoardService
    from cairn.runtime.task_board.tools import TaskTodoTool

    service = TaskBoardService(TaskBoardStore(tmp_path / "tasks"))
    task = service.create("todo idempotence")
    with_todo = service.add_todo(task.task_id, "once")
    assert with_todo is not None
    todo_id = with_todo.todos[0].todo_id
    tool = TaskTodoTool(service)

    async def flow() -> None:
        first = await tool.execute(
            {"task_id": task.task_id, "todo_id": todo_id, "done": True}
        )
        second = await tool.execute(
            {"task_id": task.task_id, "todo_id": todo_id, "done": True}
        )
        assert first.exit_code == 0 and second.exit_code == 0
        assert "once" in second.stdout
        missing = await tool.execute(
            {"task_id": task.task_id, "todo_id": "missing", "done": True}
        )
        assert missing.exit_code != 0

    asyncio.run(flow())


def test_oversized_todo_and_progress_leave_existing_task_intact(tmp_path: Path) -> None:
    store = TaskBoardStore(tmp_path / "tasks")
    task = store.create(title="Keep details")
    before = store.load()

    with pytest.raises(TaskBoardValidationError, match="at most 2000"):
        store.add_todo(task.task_id, "字" * 2001)
    with pytest.raises(TaskBoardValidationError, match="at most 2000"):
        store.update(task.task_id, progress="x" * 2001)
    assert store.load() == before

    with_todo = store.add_todo(task.task_id, "字" * 2000)
    assert with_todo is not None and with_todo.todos[0].text == "字" * 2000
    with_progress = store.update(task.task_id, progress="x" * 2000)
    assert with_progress is not None and with_progress.progress == ("x" * 2000,)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("task_create", {"title": "x" * 301}),
        ("task_create", {"title": "valid", "description": "x" * 8001}),
        ("task_update", {"task_id": "task-1", "description": "x" * 8001}),
        ("task_update", {"task_id": "task-1", "progress": "x" * 2001}),
        ("task_todo", {"task_id": "task-1", "text": "字" * 2001}),
    ],
)
def test_oversized_task_tool_arguments_fail_before_permission(
    tmp_path: Path, tool_name: str, arguments: dict[str, str]
) -> None:
    import json

    from cairn.core.models import LLMResponse, ToolCall
    from cairn.infra.observability.tracer import Tracer
    from cairn.runtime.loop import run_turn
    from tests.support.runtime import (
        TEST_BUDGET,
        RecordingSink,
        SequenceLLM,
        make_agent,
    )

    store = TaskBoardStore(tmp_path / "tasks")
    service = TaskBoardService(store)
    tools: dict[str, Tool] = {
        "task_create": TaskCreateTool(service),
        "task_update": TaskUpdateTool(service),
        "task_todo": TaskTodoTool(service),
    }
    tool = tools[tool_name]
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="invalid", name=tool_name, arguments=arguments)]
            ),
            LLMResponse(content="recovered"),
        ]
    )
    sink = RecordingSink()
    agent = make_agent(llm, tool, [])
    agent.tracer = Tracer(sink)
    assert asyncio.run(run_turn(agent, "try oversized input", budget=TEST_BUDGET)) == (
        "recovered"
    )
    failure = json.loads(agent.state.messages[2].content or "")
    assert failure["type"] == "InvalidArguments"
    assert "at most" in failure["error"]
    assert not [span for span in sink.spans if span.name == "permission.check"]
    assert not [span for span in sink.spans if span.name == "tool.execute"]
    assert not store.path.exists()


def test_cli_rejects_oversized_todo_without_truncating(tmp_path: Path) -> None:
    from cairn.terminal.task_board.controller import TaskBoardController

    store = TaskBoardStore(tmp_path / "tasks")
    service = TaskBoardService(store)
    controller = TaskBoardController(service)
    task = service.create("Keep details")

    async def flow() -> None:
        result = await controller.command(f"todo {task.task_id} " + "x" * 2001)
        assert "at most 2000" in result.message
        assert store.load()[0].todos == ()

    asyncio.run(flow())


@pytest.mark.parametrize("write_fails", [False, True])
def test_repeated_cancellation_waits_for_update_and_preserves_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_fails: bool
) -> None:
    store = TaskBoardStore(tmp_path / "tasks")
    service = TaskBoardService(store)
    task = service.create("Preserve update outcome")
    started = threading.Event()
    release = threading.Event()
    update = service.update

    def delayed_update(*args: Any, **kwargs: Any) -> TaskBoardItem | None:
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("Test failed to release the update")
        if write_fails:
            raise OSError("sensitive write failure")
        return update(*args, **kwargs)

    monkeypatch.setattr(service, "update", delayed_update)

    async def flow() -> None:
        pending = asyncio.create_task(
            service.aupdate(task.task_id, progress="finished")
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            pending.cancel("first")
            await asyncio.sleep(0)
            pending.cancel("second")
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert caught.value.args == ("first",)
        notes = " ".join(getattr(caught.value, "__notes__", ()))
        assert "cancellation" in notes.lower()
        if write_fails:
            assert "OSError" in notes
            assert "sensitive write failure" not in notes
            assert store.load()[0].progress == ()
        else:
            assert store.load()[0].progress == ("finished",)

    asyncio.run(flow())
