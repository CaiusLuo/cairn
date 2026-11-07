"""Online Conversation/Run execution for ordinary chat.

DomainStore is the write authority for conversation and run facts. RuntimeTask
is used only as a transient adapter for the existing LangGraph execution loop;
it is never written to RuntimeCatalogue on this path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from cairn.core.domain import (
    Conversation,
    ConversationSource,
    Run,
    RunEvent,
    RunStatus,
    workspace_id_for,
)
from cairn.infra.domain_store import DomainStore
from cairn.infra.runtime_catalog import RuntimeTask
from cairn.infra.sessions.models import CheckpointReceipt
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.service_execution import ServiceExecution
from cairn.runtime.task_service import TaskAdvanced, TaskNeedsRecovery, TaskPaused


class ConversationBusy(RuntimeError):
    """The V1 conversation already has an active Run."""


class RunLedger:
    """Transient execution facts backed by one DomainStore Run."""

    def __init__(
        self,
        store: DomainStore,
        run: Run,
        *,
        history_origins: tuple[CheckpointReceipt, ...] = (),
        session_id: str | None = None,
    ) -> None:
        self.store = store
        self.run_id = run.run_id
        self.task = RuntimeTask(
            task_id=run.run_id,
            prompt=run.prompt or "message",
            source=run.source.value if run.source is not None else "cli",
            mode="conversation",
            conversation_key=run.conversation_id,
            input_prompt=run.prompt or None,
            session_id=session_id,
            history_origins=history_origins,
        )

    def inspect(self, task_id: str) -> RuntimeTask:
        if task_id != self.run_id:
            raise KeyError(task_id)
        return self.task

    def record(
        self, task: RuntimeTask, kind: str, data: dict[str, Any] | None = None
    ) -> RuntimeTask:
        updated = task.model_copy(update={"sequence": task.sequence + 1})
        self.task = updated
        self.store.append_run_event(self.run_id, kind, data or {})
        run = self.store.get_run(self.run_id)
        if (
            run is not None
            and updated.session_id is not None
            and run.session_id != updated.session_id
        ):
            self.store.update_run(self.run_id, session_id=updated.session_id)
        return updated


class ConversationService:
    """Owns conversation/run admission and the in-process run queue."""

    def __init__(
        self,
        store: DomainStore,
        execution: ServiceExecution,
        *,
        workspace: Path,
        queue_size: int = 64,
    ) -> None:
        self.store = store
        self.execution = execution
        self.workspace = workspace.resolve()
        self.workspace_id = workspace_id_for(self.workspace)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.worker: asyncio.Task[None] | None = None
        self.active_run_id: str | None = None
        self.active_task: asyncio.Task[None] | None = None
        self.cancel_requested: set[str] = set()
        self.stopping = False
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.changed = asyncio.Event()

    async def start(self) -> None:
        if self.worker is not None:
            raise RuntimeError("Conversation service already started")
        for run in self.store.list_runs():
            if run.workspace_id != self.workspace_id:
                continue
            if run.status is RunStatus.RUNNING:
                self.store.update_run(
                    run.run_id,
                    status=RunStatus.RECOVERY_REQUIRED,
                    error="process_interrupted",
                )
            elif run.status is RunStatus.QUEUED:
                self.queue.put_nowait(run.run_id)
        self.worker = asyncio.create_task(self._work())

    def create_conversation(
        self, prompt: str, source: ConversationSource
    ) -> Conversation:
        return self.store.create_conversation(
            workspace=self.workspace,
            source=source,
            initial_prompt=prompt,
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        conversation = self.store.get_conversation(conversation_id)
        if conversation is not None and conversation.workspace_id != self.workspace_id:
            return None
        return conversation

    def list_conversations(self) -> tuple[Conversation, ...]:
        return self.store.list_conversations(workspace_id=self.workspace_id)

    def get_run(self, run_id: str) -> Run | None:
        run = self.store.get_run(run_id)
        if run is not None and run.workspace_id != self.workspace_id:
            return None
        return run

    def runs_for_conversation(self, conversation_id: str) -> tuple[Run, ...]:
        if self.get_conversation(conversation_id) is None:
            return ()
        return self.store.list_runs_for_conversation(conversation_id)

    def submit(
        self,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
    ) -> Run:
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Conversation runtime is not accepting runs")
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        existing = self.store.get_run_by_request(conversation_id, request_id)
        if existing is None:
            active = any(
                run.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                for run in self.store.list_runs_for_conversation(conversation_id)
            )
            if active:
                raise ConversationBusy("Conversation already has an active Run")
        if existing is None and self.queue.full():
            raise asyncio.QueueFull
        run = self.store.accept_run(
            conversation_id=conversation_id,
            prompt=prompt,
            source=source,
            request_id=request_id,
        )
        if existing is None:
            self.queue.put_nowait(run.run_id)
        return run

    def events(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        if self.get_run(run_id) is None:
            raise ValueError("Run not found")
        return self.store.list_run_events(run_id, after)

    async def follow(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        events = self.events(run_id, after)
        if events:
            return events
        self.changed.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(self.changed.wait(), 15)
        return self.events(run_id, after)

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        if self.get_conversation(conversation_id) is None:
            return []
        runs = [
            run
            for run in self.store.list_runs_for_conversation(conversation_id)
            if run.session_id is not None
        ]
        if not runs:
            return []
        latest = runs[-1]
        task = RuntimeTask(
            task_id=latest.run_id,
            prompt=latest.prompt or "message",
            source="cli",
            mode="conversation",
            conversation_key=conversation_id,
            session_id=latest.session_id,
        )
        return self.execution.history(task)

    def publish(self, run_id: str, message: dict[str, Any]) -> None:
        for queue in tuple(self.subscribers.get(run_id, ())):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync"})
                self.subscribers[run_id].discard(queue)
            else:
                queue.put_nowait(message)

    @asynccontextmanager
    async def subscribe(
        self, run_id: str
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        self.get_run(run_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self.subscribers.setdefault(run_id, set()).add(queue)
        try:
            yield queue
        finally:
            queues = self.subscribers.get(run_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    del self.subscribers[run_id]

    async def cancel(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status is RunStatus.QUEUED:
            self.store.update_run(
                run_id, status=RunStatus.CANCELLED, error="cancelled", ended_at=_now()
            )
        elif run.status is RunStatus.RUNNING and self.active_run_id == run_id:
            self.cancel_requested.add(run_id)
            if self.active_task is not None:
                self.active_task.cancel()
        else:
            raise ValueError("Run is not cancellable")
        updated = self.get_run(run_id)
        assert updated is not None
        return updated

    async def _work(self) -> None:
        while True:
            run_id = await self.queue.get()
            task = asyncio.create_task(self._run(run_id))
            self.active_task = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self.active_task = None
                self.queue.task_done()

    def _prior_run(self, run: Run) -> Run | None:
        prior = [
            candidate
            for candidate in self.store.list_runs_for_conversation(run.conversation_id)
            if candidate.run_id != run.run_id and candidate.session_id is not None
        ]
        return prior[-1] if prior else None

    async def _run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run is None or run.status is not RunStatus.QUEUED:
            return
        self.active_run_id = run_id
        self.store.update_run(run_id, status=RunStatus.RUNNING)
        prior = self._prior_run(run)
        origins: tuple[CheckpointReceipt, ...] = ()
        if prior is not None and prior.checkpoint_receipt is not None:
            with suppress(ValueError):
                origins = (CheckpointReceipt.model_validate(prior.checkpoint_receipt),)
        ledger = RunLedger(
            self.store,
            run,
            history_origins=origins,
            session_id=run.session_id or (prior.session_id if prior else None),
        )

        def emit(kind: str, data: dict[str, Any]) -> None:
            current = ledger.inspect(run_id)
            ledger.record(current, kind, data)
            if kind == "model_text_delta":
                self.publish(run_id, {"type": "preview", "text": data["text"]})
            else:
                self.publish(run_id, {"type": "event", "kind": kind, "data": data})

        try:
            result = await self.execution(ledger.task, emit, service=ledger)
            self.store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
                result=result,
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
        except TaskPaused as exc:
            self.store.update_run(
                run_id,
                status=RunStatus.PAUSED,
                error=str(exc),
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
        except TaskNeedsRecovery:
            self.store.update_run(
                run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="effects_require_host_review",
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
        except TaskAdvanced:
            self.store.update_run(
                run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="development_handoff_requires_B3",
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
        except asyncio.CancelledError:
            self.store.update_run(
                run_id,
                status=(
                    RunStatus.CANCELLED
                    if run_id in self.cancel_requested
                    else RunStatus.RECOVERY_REQUIRED
                ),
                error=(
                    "cancelled"
                    if run_id in self.cancel_requested
                    else "execution_cancelled"
                ),
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
            raise
        except Exception as exc:
            self.store.update_run(
                run_id,
                status=RunStatus.FAILED,
                error=type(exc).__name__,
                session_id=ledger.task.session_id,
                ended_at=_now(),
            )
        finally:
            self.cancel_requested.discard(run_id)
            self.active_run_id = None
            self.changed.set()
            self.publish(run_id, {"type": "settled"})

    async def close(self) -> None:
        self.stopping = True
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
            await settle_owned(self.active_task)
        if self.worker is not None:
            outcome = await settle_owned(self.worker)
            if outcome.cancellation is not None:
                raise outcome.cancellation
        for run_id in tuple(self.subscribers):
            self.publish(run_id, {"type": "shutdown"})


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = ["ConversationBusy", "ConversationService", "RunLedger"]
