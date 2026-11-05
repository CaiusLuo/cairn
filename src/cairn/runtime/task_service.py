"""One long-lived owner of task admission, workers and public execution facts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from cairn.core.task_board import TaskBoardItem, TaskBoardStatus, TaskSource
from cairn.infra.runtime_catalog import (
    RuntimeCatalogue,
    RuntimeEvent,
    RuntimeSource,
    RuntimeTask,
)
from cairn.runtime.lifecycle import await_owned, settle_owned
from cairn.runtime.task_board.service import TaskBoardService

TaskExecutor = Callable[
    [RuntimeTask, Callable[[str, dict[str, Any]], None]], Coroutine[Any, Any, str]
]


class TaskPaused(Exception):
    """A settled turn stopped at a safe budget boundary."""


class TaskService:
    def __init__(
        self,
        catalogue: RuntimeCatalogue,
        executor: TaskExecutor,
        *,
        queue_size: int = 64,
        board: TaskBoardService | None = None,
    ) -> None:
        self.board = board
        self.projections: dict[str, RuntimeTask] = {}
        self.projection_signal = asyncio.Event()
        self.projector: asyncio.Task[None] | None = None
        self.projection_error: str | None = None
        self.history_reader: Callable[[RuntimeTask], list[dict[str, Any]]] | None = None
        self.catalogue = catalogue
        self.executor = executor
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.worker: asyncio.Task[None] | None = None
        self.active: asyncio.Task[str] | None = None
        self.active_id: str | None = None
        self.stopping = False
        self.changed = asyncio.Event()

    async def start(self) -> None:
        if self.worker is not None:
            raise RuntimeError("Runtime already started")
        for task in list(self.catalogue.data.tasks.values()):
            if task.status == "running":
                self.record(
                    task.model_copy(
                        update={
                            "status": "recovery_required",
                            "diagnostic": "process_interrupted",
                        }
                    ),
                    "interrupted",
                )
            elif task.status == "queued":
                self.queue.put_nowait(task.task_id)
        self.worker = asyncio.create_task(self._work())
        if self.board is not None:
            self.projections.update({task.task_id: task for task in self.list_tasks()})
            self.projection_signal.set()
            self.projector = asyncio.create_task(self._project())

    def inspect(self, task_id: str) -> RuntimeTask:
        return self.catalogue.data.tasks[task_id]

    def list_tasks(self) -> list[RuntimeTask]:
        return list(self.catalogue.data.tasks.values())

    def record(
        self, task: RuntimeTask, kind: str, data: dict[str, Any] | None = None
    ) -> RuntimeTask:
        previous = self.catalogue.data.tasks.get(task.task_id)
        updated = self.catalogue.record(task, kind, data)
        if self.board is not None and (
            previous is None
            or previous.status != updated.status
            or kind == "worktree_ready"
        ):
            self.projections[task.task_id] = updated
            self.projection_signal.set()
        self.changed.set()
        return updated

    def submit(
        self, prompt: str, source: RuntimeSource, request_id: str | None = None
    ) -> RuntimeTask:
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Runtime is not accepting tasks")
        if request_id is not None:
            for previous in self.list_tasks():
                if previous.request_id == request_id:
                    if previous.prompt != prompt or previous.source != source:
                        raise ValueError(
                            "Source message was already bound to another request"
                        )
                    return previous
        if self.queue.full():
            raise asyncio.QueueFull
        task = RuntimeTask(prompt=prompt, source=source, request_id=request_id)
        task = self.record(task, "accepted", {"source": source})
        self.queue.put_nowait(task.task_id)
        return task

    def events(self, task_id: str, after: int = 0) -> list[RuntimeEvent]:
        task = self.inspect(task_id)
        if after < 0 or after > task.sequence:
            raise ValueError("Invalid event cursor")
        return [
            event
            for event in self.catalogue.data.events[task_id]
            if event.sequence > after
        ][:200]

    async def follow(self, task_id: str, after: int = 0) -> list[RuntimeEvent]:
        events = self.events(task_id, after)
        if events:
            return events
        self.changed.clear()
        # No await separates inspection and clear, so notification cannot be lost.
        with suppress(TimeoutError):
            await asyncio.wait_for(self.changed.wait(), 15)
        return self.events(task_id, after)

    async def cancel(self, task_id: str) -> RuntimeTask:
        task = self.inspect(task_id)
        if task.status == "queued":
            return self.record(
                task.model_copy(update={"status": "cancelled"}), "cancelled"
            )
        if task_id == self.active_id and self.active is not None:
            await settle_owned(self.active)
            while self.active_id == task_id:
                self.changed.clear()
                await self.changed.wait()
        return self.inspect(task_id)

    def continue_task(self, task_id: str) -> RuntimeTask:
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Runtime is not accepting tasks")
        task = self.inspect(task_id)
        if task.status != "paused":
            raise ValueError(
                "Only a safely paused task can continue; recovery requires host reconciliation"
            )
        if self.queue.full():
            raise asyncio.QueueFull
        task = self.record(task.model_copy(update={"status": "queued"}), "continued")
        self.queue.put_nowait(task_id)
        return task

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                task = self.inspect(task_id)
                if task.status != "queued":
                    continue
                task = self.record(
                    task.model_copy(
                        update={"status": "running", "attempts": task.attempts + 1}
                    ),
                    "running",
                )

                def emit(
                    kind: str, data: dict[str, Any], task_id: str = task_id
                ) -> None:
                    self.record(self.inspect(task_id), kind, data)

                self.active_id = task_id
                self.active = asyncio.create_task(self.executor(task, emit))
                try:
                    result = await asyncio.shield(self.active)
                except asyncio.CancelledError:
                    await settle_owned(self.active)
                    self.record(
                        self.inspect(task_id).model_copy(
                            update={
                                "status": "recovery_required",
                                "diagnostic": "execution_cancelled",
                            }
                        ),
                        "interrupted",
                    )
                    if self.stopping:
                        raise
                except TaskPaused as exc:
                    self.record(
                        self.inspect(task_id).model_copy(
                            update={"status": "paused", "result": str(exc)}
                        ),
                        "paused",
                    )
                except Exception as exc:
                    self.record(
                        self.inspect(task_id).model_copy(
                            update={
                                "status": "failed",
                                "diagnostic": type(exc).__name__,
                            }
                        ),
                        "failed",
                    )
                else:
                    self.record(
                        self.inspect(task_id).model_copy(
                            update={"status": "completed", "result": result}
                        ),
                        "completed",
                    )
                finally:
                    self.active = None
                    self.active_id = None
            finally:
                self.queue.task_done()

    async def _project(self) -> None:
        assert self.board is not None
        statuses = {"paused": "waiting", "recovery_required": "waiting"}
        while True:
            await self.projection_signal.wait()
            self.projection_signal.clear()
            while self.projections:
                _, task = self.projections.popitem()
                now = datetime.now(UTC).isoformat()
                item = TaskBoardItem(
                    task_id=task.task_id,
                    project=self.board.project,
                    title=task.prompt[:300],
                    description=task.prompt[:8000],
                    source=TaskSource(task.source),
                    session_id=task.session_id or "",
                    status=TaskBoardStatus(statuses.get(task.status, task.status)),
                    progress=(
                        "执行状态: " + task.status,
                        "Worktree: " + (task.worktree or "准备中"),
                    ),
                    created_at=now,
                    updated_at=now,
                )
                try:
                    await self.board.project_execution(item)
                    self.projection_error = None
                except Exception as exc:
                    self.projection_error = type(exc).__name__
            if self.stopping:
                return

    async def close(self) -> None:
        self.stopping = True
        if self.worker is not None:
            await settle_owned(self.worker)
        if self.projector is not None:
            self.projection_signal.set()
            await await_owned(self.projector)
            self.projector.result()
        self.catalogue.close()
