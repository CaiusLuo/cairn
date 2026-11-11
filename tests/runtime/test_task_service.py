import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.development.board.service import TaskBoardService
from cairn.development.board.store import TaskBoardStore
from cairn.development.catalogue import (
    INPUT_EXCERPT_LIMIT,
    RuntimeCatalogue,
    RuntimeTask,
)
from cairn.development.service import TaskService


def test_one_owner_queue_and_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = RuntimeCatalogue(tmp_path / "state", forbidden_roots=())
        with pytest.raises(RuntimeError, match="Another runtime"):
            RuntimeCatalogue(tmp_path / "state", forbidden_roots=())
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def execute(
            task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
        ) -> str:
            calls.append(task.task_id)
            started.set()
            await release.wait()
            emit("tool", {"name": "read_file"})
            return "done"

        service = TaskService(store, execute)
        await service.start()
        first = service.submit("read", "cli")
        await started.wait()
        second = service.submit("also read", "web")
        assert service.inspect(first.task_id).status == "running"
        assert service.inspect(second.task_id).status == "queued"
        assert (await service.cancel(second.task_id)).status == "cancelled"
        release.set()
        await asyncio.wait_for(service.queue.join(), 2)
        assert service.inspect(first.task_id).result == "done"
        events = service.events(first.task_id)
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        assert service.events(first.task_id, events[-2].sequence) == events[-1:]
        assert calls == [first.task_id]
        await service.close()

    asyncio.run(scenario())


def test_restart_preserves_interruption_without_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        store = RuntimeCatalogue(root, forbidden_roots=())
        task = store.record(
            RuntimeTask(
                prompt="edit",
                source="wechat",
                status="running",
                worktree="/kept/worktree",
            ),
            "running",
        )
        store.close()
        calls: list[str] = []

        async def execute(
            task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
        ) -> str:
            calls.append(task.task_id)
            return "unexpected"

        service = TaskService(RuntimeCatalogue(root, forbidden_roots=()), execute)
        await service.start()
        record = service.inspect(task.task_id)
        assert record.status == "recovery_required"
        assert record.worktree == "/kept/worktree"
        with pytest.raises(ValueError, match="reconciliation"):
            service.continue_task(task.task_id)
        await asyncio.sleep(0)
        assert calls == []
        await service.close()

    asyncio.run(scenario())


def test_input_receipt_redacts_before_truncating_and_reloads(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "state"
        marker = "secretmarker"
        secret = marker + "s3cr3t-boundary-s3cr3t"
        payload = "x" * 990 + secret + "y" * 100

        async def execute(
            task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
        ) -> str:
            return payload

        service = TaskService(
            RuntimeCatalogue(root, forbidden_roots=(), sensitive_values=(secret,)),
            execute,
        )
        await service.start()
        first = service.submit("first", "cli")
        await asyncio.wait_for(service.queue.join(), 2)
        service.message(
            first.task_id,
            "second",
            "cli",
            "redact-1",
            service.inspect(first.task_id).sequence,
        )
        await asyncio.wait_for(service.queue.join(), 2)

        completed = service.inspect(first.task_id)
        receipt = completed.inputs[-1]
        excerpt = receipt.result_excerpt or ""
        assert receipt.request_id == "redact-1"
        # The secret spans the truncation boundary; redacting after truncating
        # would leave a readable prefix in the durable receipt.
        assert secret not in excerpt
        assert marker[:10] not in excerpt
        assert len(excerpt) <= INPUT_EXCERPT_LIMIT
        assert receipt.result_truncated is True
        await service.close()

        reopened = RuntimeCatalogue(root, forbidden_roots=())
        try:
            assert reopened.data.tasks[first.task_id].inputs == completed.inputs
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_bad_state_is_not_silently_replaced(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "runtime.json").write_text("broken")
    with pytest.raises(ValueError):
        RuntimeCatalogue(root, forbidden_roots=())
    assert (root / "runtime.json").read_text() == "broken"
    with pytest.raises(ValueError, match="outside"):
        RuntimeCatalogue(
            tmp_path / "agent" / "state", forbidden_roots=(tmp_path / "agent",)
        )


def test_slow_subscriber_is_bounded_and_cannot_block_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def execute(
            task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
        ) -> str:
            for _ in range(1000):
                emit("model_text_delta", {"text": "transient"})
            return "done"

        service = TaskService(
            RuntimeCatalogue(tmp_path / "state", forbidden_roots=()), execute
        )
        await service.start()
        task = service.submit("stream", "cli")
        async with service.subscribe(task.task_id) as queue:
            await asyncio.wait_for(service.queue.join(), 2)
            assert service.inspect(task.task_id).status == "completed"
            assert queue.qsize() == 1 and queue.get_nowait()["type"] == "resync"
            assert len(service.catalogue.data.events[task.task_id]) == 3
        assert service.subscribers == {}
        await service.close()

    asyncio.run(scenario())


def test_conversation_runs_are_not_projected_as_task_board_cards(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        board = TaskBoardService(TaskBoardStore(tmp_path / "board"))

        async def execute(
            task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
        ) -> str:
            del task, emit
            return "done"

        service = TaskService(
            RuntimeCatalogue(tmp_path / "state", forbidden_roots=()),
            execute,
            board=board,
        )
        await service.start()
        chat = service.submit("hello", "cli", mode="conversation")
        await asyncio.wait_for(service.queue.join(), 2)
        assert chat.mode == "conversation"
        assert board.store.load() == ()

        dev = service.submit("build", "cli", mode="development")
        await asyncio.wait_for(service.queue.join(), 2)
        for _ in range(20):
            if board.store.load():
                break
            await asyncio.sleep(0.01)
        assert [item.task_id for item in board.store.load()] == [dev.task_id]
        await service.close()

    asyncio.run(scenario())
