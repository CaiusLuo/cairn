import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.runtime_catalog import RuntimeCatalogue, RuntimeTask
from cairn.runtime.task_service import TaskService


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
