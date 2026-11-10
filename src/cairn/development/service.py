"""One long-lived owner of task admission, workers and public execution facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cairn.agent.session.models import CheckpointReceipt
from cairn.conversation.store import DomainStore
from cairn.development.board.models import TaskBoardItem, TaskBoardStatus, TaskSource
from cairn.development.board.service import TaskBoardService
from cairn.development.catalogue import (
    RuntimeCatalogue,
    RuntimeEvent,
    RuntimeSource,
    RuntimeTask,
)
from cairn.development.models import TaskStatus
from cairn.shared.lifecycle import await_owned, settle_owned

TaskExecutor = Callable[
    [RuntimeTask, Callable[[str, dict[str, Any]], None]], Coroutine[Any, Any, str]
]


class TaskPaused(Exception):
    """A settled turn stopped at a safe budget boundary."""


class TaskAdvanced(Exception):
    """A committed conversation requested the next development phase."""


class TaskNeedsRecovery(Exception):
    """Observed effects require explicit host review before another turn."""


class TaskService:
    def __init__(
        self,
        catalogue: RuntimeCatalogue,
        executor: TaskExecutor,
        *,
        queue_size: int = 64,
        board: TaskBoardService | None = None,
    ) -> None:
        self.validate_submission: Callable[[RuntimeTask], None] | None = None
        self.default_model_id: str | None = None
        self.on_settled: Callable[[RuntimeTask], None] | None = None
        self.board = board
        self.projections: dict[str, RuntimeTask] = {}
        self.projection_signal = asyncio.Event()
        self.projector: asyncio.Task[None] | None = None
        self.projection_error: str | None = None
        self.history_reader: Callable[[RuntimeTask], list[dict[str, Any]]] | None = None
        self.recovery_reader: Callable[[RuntimeTask], dict[str, Any]] | None = None
        self.reconciler: Callable[[RuntimeTask, dict[str, Any]], None] | None = None
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.catalogue = catalogue
        self.executor = executor
        self.domain_store: DomainStore | None = None
        self.domain_projection_error: str | None = None
        self.projection_failures: dict[str, int] = {}
        self.projection_retry_tasks: set[asyncio.Task[None]] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.worker: asyncio.Task[None] | None = None
        self.active: asyncio.Task[str] | None = None
        self.active_id: str | None = None
        self.stopping = False
        self.changed = asyncio.Event()

    async def start(self) -> None:
        if self.worker is not None:
            raise RuntimeError("Runtime already started")
        self.projection_failures.clear()
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
        if self.board is not None or self.domain_store is not None:
            self.projections.update(
                {
                    task.task_id: task
                    for task in self.list_tasks()
                    if task.mode == "development"
                    or task.development_prompt is not None
                    or task.worktree is not None
                }
            )
            self.projection_signal.set()
            self.projector = asyncio.create_task(self._project())

    def inspect(self, task_id: str) -> RuntimeTask:
        return self.catalogue.data.tasks[task_id]

    def inspect_wechat_task(
        self, task_id: str, *, sender: str | None, account: str | None
    ) -> RuntimeTask:
        task = self.inspect(task_id)
        if (
            task.source != "wechat"
            or not sender
            or not account
            or task.delivery_recipient != sender
            or task.delivery_account != account
        ):
            raise KeyError(task_id)
        return task

    def list_tasks(self) -> list[RuntimeTask]:
        return list(self.catalogue.data.tasks.values())

    def record(
        self, task: RuntimeTask, kind: str, data: dict[str, Any] | None = None
    ) -> RuntimeTask:
        previous = self.catalogue.data.tasks.get(task.task_id)
        updated = self.catalogue.record(task, kind, data)
        if (self.board is not None or self.domain_store is not None) and (
            previous is None
            or previous.status != updated.status
            or previous.mode != updated.mode
            or previous.worktree != updated.worktree
            or previous.branch != updated.branch
            or kind == "worktree_ready"
        ):
            self.projections[task.task_id] = updated
            self.projection_signal.set()
        self.changed.set()
        self.publish(
            task.task_id,
            {
                "type": "event",
                "event": self.catalogue.data.events[task.task_id][-1].model_dump(
                    mode="json"
                ),
            },
        )
        return updated

    def publish(self, task_id: str, message: dict[str, Any]) -> None:
        """Bounded transient fanout; a slow reader reconnects using durable cursors."""
        for queue in tuple(self.subscribers.get(task_id, ())):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync"})
                self.subscribers[task_id].discard(queue)
            else:
                queue.put_nowait(message)

    @asynccontextmanager
    async def subscribe(
        self, task_id: str
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        self.inspect(task_id)
        if sum(len(queues) for queues in self.subscribers.values()) >= 32:
            raise RuntimeError("Runtime subscriber capacity reached")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self.subscribers.setdefault(task_id, set()).add(queue)
        try:
            yield queue
        finally:
            queues = self.subscribers.get(task_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    del self.subscribers[task_id]

    def recovery(self, task_id: str) -> dict[str, Any]:
        task = self.inspect(task_id)
        if task.status != "recovery_required" or self.recovery_reader is None:
            raise ValueError("Task does not require recovery")
        return {
            "task_id": task_id,
            "expected_sequence": task.sequence,
            **self.recovery_reader(task),
        }

    def reconcile(self, task_id: str, report: dict[str, Any]) -> RuntimeTask:
        task = self.inspect(task_id)
        if (
            task.status != "recovery_required"
            or task_id == self.active_id
            or self.reconciler is None
            or report.get("expected_sequence") != task.sequence
        ):
            raise ValueError("Fresh recovery review required")
        self.reconciler(task, report)
        return self.record(
            task.model_copy(
                update={
                    "status": "paused",
                    "diagnostic": None,
                    "recovery_note": report["note"],
                }
            ),
            "reconciled",
        )

    def submit(
        self,
        prompt: str,
        source: RuntimeSource,
        request_id: str | None = None,
        *,
        mode: str = "development",
        conversation_key: str | None = None,
        delivery_recipient: str | None = None,
        delivery_account: str | None = None,
        history_origins: tuple[CheckpointReceipt, ...] = (),
    ) -> RuntimeTask:
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Runtime is not accepting tasks")
        if request_id is not None:
            for previous in self.list_tasks():
                if previous.request_id == request_id:
                    if (
                        previous.prompt != prompt
                        or previous.source != source
                        or previous.mode != mode
                        or previous.conversation_key != conversation_key
                        or previous.delivery_recipient != delivery_recipient
                        or previous.delivery_account != delivery_account
                        or previous.history_origins != history_origins
                    ):
                        raise ValueError(
                            "Source message was already bound to another request"
                        )
                    return previous
        if self.queue.full():
            raise asyncio.QueueFull
        task = RuntimeTask.model_validate(
            {
                "prompt": prompt,
                "source": source,
                "request_id": request_id,
                "input_id": request_id or "input:" + uuid4().hex,
                "mode": mode,
                "conversation_key": conversation_key,
                "model_id": self.default_model_id,
                "delivery_recipient": delivery_recipient,
                "delivery_account": delivery_account,
                "history_origins": history_origins,
            }
        )
        validator = self.validate_submission
        if validator is not None:
            validator(task)
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
        task = self.record(
            task.model_copy(
                update={
                    "status": "queued",
                    "resume_requested": True,
                    "metrics": {**task.metrics, "window": {}},
                }
            ),
            "continued",
        )
        self.queue.put_nowait(task_id)
        return task

    def message(
        self,
        task_id: str,
        prompt: str,
        source: RuntimeSource,
        request_id: str,
        expected_sequence: int,
        *,
        delivery_recipient: str | None = None,
        delivery_account: str | None = None,
    ) -> RuntimeTask:
        task = (
            self.inspect_wechat_task(
                task_id, sender=delivery_recipient, account=delivery_account
            )
            if source == "wechat"
            else self.inspect(task_id)
        )
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Runtime is not accepting messages")
        fingerprint = hashlib.sha256(
            json.dumps(
                [prompt, source, delivery_recipient, delivery_account],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        if request_id in task.input_receipts:
            if task.input_receipts[request_id] != fingerprint:
                raise ValueError("Message id already belongs to another input")
            return task
        if task.sequence != expected_sequence or task.status not in {
            "completed",
            "paused",
        }:
            raise ValueError(
                "Refresh history before sending; running/recovery tasks cannot accept a new turn"
            )
        if self.queue.full():
            raise asyncio.QueueFull
        candidate = RuntimeTask.model_validate(
            {
                **task.model_dump(),
                "status": "queued",
                "input_prompt": prompt,
                "input_id": request_id,
                "delivery_recipient": delivery_recipient,
                "delivery_account": delivery_account,
                "resume_requested": False,
                "development_prompt": task.development_prompt
                if task.worktree
                else None,
                "metrics": {**task.metrics, "window": {}},
                "input_receipts": {**task.input_receipts, request_id: fingerprint},
                "result": None,
                "diagnostic": None,
            }
        )
        validator = self.validate_submission
        if validator is not None:
            validator(candidate)
        updated = self.record(
            candidate, "message_accepted", {"source": source, "request_id": request_id}
        )
        self.queue.put_nowait(task_id)
        return updated

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                await self._run_task(task_id)
            finally:
                self.queue.task_done()

    async def _run_task(self, task_id: str, *, advanced: bool = False) -> None:
        task = self.inspect(task_id)
        if task.status != "queued":
            return
        task = self.record(
            task.model_copy(
                update={"status": "running", "attempts": task.attempts + 1}
            ),
            "running",
        )

        def emit(kind: str, data: dict[str, Any], task_id: str = task_id) -> None:
            if kind == "model_text_delta":
                self.publish(task_id, {"type": "preview", "text": data["text"]})
            else:
                current = self.inspect(task_id)
                if kind == "agent_step" and "budget" in data:
                    current = current.model_copy(
                        update={
                            "metrics": {
                                **current.metrics,
                                "live": data["budget"],
                            }
                        }
                    )
                self.record(current, kind, data)

        advance = False
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
        except TaskAdvanced:
            if advanced:
                self.record(
                    self.inspect(task_id).model_copy(
                        update={
                            "status": "recovery_required",
                            "diagnostic": "repeated_handoff",
                        }
                    ),
                    "interrupted",
                )
            else:
                self.record(
                    self.inspect(task_id).model_copy(
                        update={"status": "queued", "mode": "development"}
                    ),
                    "development_queued",
                )
                advance = True
        except TaskNeedsRecovery:
            self.record(
                self.inspect(task_id).model_copy(
                    update={
                        "status": "recovery_required",
                        "diagnostic": "effects_require_host_review",
                    }
                ),
                "interrupted",
            )
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
            if not advance:
                self.publish(task_id, {"type": "settled"})
                if self.on_settled is not None:
                    self.on_settled(self.inspect(task_id))
            self.active = None
            self.active_id = None
            self.changed.set()
        if advance and not self.stopping:
            await self._run_task(task_id, advanced=True)

    @staticmethod
    def _execution_status(task: RuntimeTask) -> str:
        return task.status

    def _domain_status(self, task: RuntimeTask) -> TaskStatus | None:
        # Business DONE/CLOSED is protected inside DomainStore's transaction.
        if task.status == "queued":
            return TaskStatus.TODO
        if task.status in {"running", "completed"}:
            return TaskStatus.IN_PROGRESS
        if task.status in {
            "paused",
            "recovery_required",
            "failed",
            "cancelled",
        }:
            return TaskStatus.WAITING
        return None

    def _project_domain(self, task: RuntimeTask) -> None:
        if self.domain_store is None:
            return
        execution_metadata = {
            "execution_status": task.status,
            "execution_task_id": task.task_id,
            "execution_sequence": task.sequence,
            "execution_diagnostic": task.diagnostic,
            "execution_result": task.result,
            "execution_session_id": task.session_id,
            "execution_worktree": task.worktree,
            "execution_branch": task.branch,
        }
        self.domain_store.project_task_execution(
            task.task_id,
            execution_metadata=execution_metadata,
            status=self._domain_status(task),
            worktree=task.worktree,
            branch=task.branch,
        )

    def _schedule_projection_retry(self, task_id: str, attempt: int) -> None:
        async def retry() -> None:
            try:
                # Bounded backoff; no tight retry loop while SQLite is unavailable.
                await asyncio.sleep(min(0.1 * (2 ** (attempt - 1)), 1.0))
                if self.stopping:
                    return
                current = self.catalogue.data.tasks.get(task_id)
                if current is not None:
                    self.projections[task_id] = current
                    self.projection_signal.set()
            finally:
                current_task = asyncio.current_task()
                if current_task is not None:
                    self.projection_retry_tasks.discard(current_task)

        retry_task = asyncio.create_task(retry())
        self.projection_retry_tasks.add(retry_task)

    async def _project(self) -> None:
        if self.board is None and self.domain_store is None:
            return
        statuses = {"paused": "waiting", "recovery_required": "waiting"}
        while True:
            await self.projection_signal.wait()
            self.projection_signal.clear()
            while self.projections:
                _, task = self.projections.popitem()
                if not (
                    task.mode == "development"
                    or task.development_prompt is not None
                    or task.worktree is not None
                ):
                    continue
                # RuntimeCatalogue is already committed by record(); projection
                # is a derived view and must never fail the execution fact.
                try:
                    await asyncio.to_thread(self._project_domain, task)
                    self.domain_projection_error = None
                    self.projection_failures.pop(task.task_id, None)
                except Exception as exc:
                    self.domain_projection_error = type(exc).__name__
                    attempt = self.projection_failures.get(task.task_id, 0) + 1
                    self.projection_failures[task.task_id] = attempt
                    if attempt <= 3:
                        self._schedule_projection_retry(task.task_id, attempt)
                if self.board is None:
                    continue
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
                        "Worktree: " + (task.worktree or "未创建"),
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
        cancellation = None
        try:
            if self.worker is not None:
                outcome = await settle_owned(self.worker)
                cancellation = outcome.cancellation
            if self.projector is not None:
                self.projection_signal.set()
                late = await await_owned(self.projector)
                cancellation = cancellation or late
                self.projector.result()
            retry_tasks = tuple(self.projection_retry_tasks)
            for retry in retry_tasks:
                retry.cancel()
            for retry in retry_tasks:
                with suppress(asyncio.CancelledError):
                    await retry
            self.projection_retry_tasks.clear()
        finally:
            for task_id in tuple(self.subscribers):
                self.publish(task_id, {"type": "shutdown"})
            self.catalogue.close()
        if cancellation is not None:
            raise cancellation
