"""Online Conversation/Run execution for ordinary chat.

DomainStore is the write authority for conversation and run facts. RuntimeTask
is used only as a transient adapter for the existing LangGraph execution loop;
it is never written to RuntimeCatalogue on this path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from cairn.core.domain import (
    Conversation,
    ConversationSource,
    Run,
    RunEvent,
    RunStatus,
    TaskStatus,
    new_task_id,
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


class ConversationRecoveryRequired(RuntimeError):
    """An earlier Run has unresolved effects and must be reviewed first."""


class RunLedger:
    """Transient execution facts backed by one DomainStore Run."""

    STREAM_FLUSH_CHARS = 8192
    PREVIEW_LIMIT = 65536

    def __init__(
        self,
        store: DomainStore,
        run: Run,
        *,
        history_origins: tuple[CheckpointReceipt, ...] = (),
        session_id: str | None = None,
        on_event: Callable[[str, bool], None] | None = None,
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
        self.preview = ""
        self.visible_text = ""
        self.trace_ref: str | None = None
        self.open_tools = 0
        self._on_event = on_event

    def _wake(self, kind: str, *, persisted: bool) -> None:
        if self._on_event is not None:
            self._on_event(kind, persisted)

    def flush_preview(self) -> None:
        if not self.preview:
            return
        text, self.preview = self.preview, ""
        self.store.append_run_event(self.run_id, "model_text_delta", {"text": text})
        self._wake("model_text_delta", persisted=True)

    def inspect(self, task_id: str) -> RuntimeTask:
        if task_id != self.run_id:
            raise KeyError(task_id)
        return self.task

    def record(
        self, task: RuntimeTask, kind: str, data: dict[str, Any] | None = None
    ) -> RuntimeTask:
        updated = task.model_copy(update={"sequence": task.sequence + 1})
        self.task = updated
        if kind == "model_text_delta":
            text = data.get("text") if data is not None else None
            if isinstance(text, str) and text:
                self.preview = (self.preview + text)[-self.PREVIEW_LIMIT :]
                self.visible_text = (self.visible_text + text)[-self.PREVIEW_LIMIT :]
                while len(self.preview) >= self.STREAM_FLUSH_CHARS:
                    self.flush_preview()
            self._wake(kind, persisted=False)
            return updated
        if kind == "model_reasoning_delta":
            self._wake(kind, persisted=False)
            return updated
        self.flush_preview()
        if kind == "tool_call":
            self.open_tools += 1
        elif kind in {"tool_result", "tool_error", "tool_denied"}:
            self.open_tools = max(0, self.open_tools - 1)
        elif kind == "trace_finish" and data is not None:
            trace_id = data.get("trace_id")
            if isinstance(trace_id, str) and trace_id:
                self.trace_ref = trace_id
        self.store.append_run_event(self.run_id, kind, data or {})
        self._wake(kind, persisted=True)
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
        legacy_service: Any | None = None,
    ) -> None:
        self.store = store
        self.execution = execution
        self.legacy_service = legacy_service
        self.workspace = workspace.resolve()
        self.workspace_id = workspace_id_for(self.workspace)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.queued_ids: set[str] = set()
        self.worker: asyncio.Task[None] | None = None
        self.active_run_id: str | None = None
        self.active_task: asyncio.Task[None] | None = None
        self.cancel_requested: set[str] = set()
        self.stopping = False
        self.subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.active_ledgers: dict[str, RunLedger] = {}
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
            elif run.status is RunStatus.QUEUED and run.run_id not in self.queued_ids:
                self.queued_ids.add(run.run_id)
                self.queue.put_nowait(run.run_id)
        self.worker = asyncio.create_task(self._work())

    def create_conversation(
        self,
        prompt: str,
        source: ConversationSource,
        request_id: str | None = None,
    ) -> Conversation:
        return self.store.create_conversation(
            workspace=self.workspace,
            source=source,
            initial_prompt=prompt,
            request_id=request_id,
        )

    def ensure_wechat_conversation(
        self,
        account_id: str,
        sender_id: str,
        initial_prompt: str,
    ) -> Conversation:
        conversation = self.store.get_or_create_wechat_conversation(
            workspace=self.workspace,
            account_id=account_id,
            sender_id=sender_id,
            initial_prompt=initial_prompt,
        )
        if conversation.workspace_id != self.workspace_id:
            raise ValueError("WeChat conversation belongs to another workspace")
        return conversation

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
            runs = self.store.list_runs_for_conversation(conversation_id)
            if any(run.status is RunStatus.RECOVERY_REQUIRED for run in runs):
                raise ConversationRecoveryRequired(
                    "Conversation has a Run requiring recovery review"
                )
            if any(run.status in {RunStatus.QUEUED, RunStatus.RUNNING} for run in runs):
                raise ConversationBusy("Conversation already has an active Run")
        if existing is None and self.queue.full():
            raise asyncio.QueueFull
        run = self.store.accept_run(
            conversation_id=conversation_id,
            prompt=prompt,
            source=source,
            request_id=request_id,
        )
        if existing is None and run.run_id not in self.queued_ids:
            self.queued_ids.add(run.run_id)
            self.queue.put_nowait(run.run_id)
        return run

    def events(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        if self.get_run(run_id) is None:
            raise ValueError("Run not found")
        return self.store.list_run_events(run_id, after)

    def cursor(self, run_id: str) -> int:
        if self.get_run(run_id) is None:
            raise ValueError("Run not found")
        return self.store.run_event_cursor(run_id)

    async def follow(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        # Clear before the read: an event written between the read and the wait
        # must not be lost.
        self.changed.clear()
        events = self.events(run_id, after)
        if events:
            return events
        run = self.get_run(run_id)
        if run is None or run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return ()
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
        task = self._runtime_task(latest)
        return self.execution.history(task)

    def _runtime_task(self, run: Run) -> RuntimeTask:
        return RuntimeTask(
            task_id=run.run_id,
            prompt=run.prompt or "message",
            source=run.source.value if run.source is not None else "cli",
            mode="conversation",
            conversation_key=run.conversation_id,
            session_id=run.session_id,
        )

    def _clean_receipt(self, session_id: str | None) -> dict[str, Any] | None:
        if session_id is None:
            return None
        try:
            report = self.execution.store.inspect(session_id)
        except Exception:
            return None
        if report.status != "clean":
            return None
        return report.receipt.model_dump(mode="json")

    def _evidence(self, ledger: RunLedger) -> dict[str, Any]:
        return {
            "budget": dict(ledger.task.metrics),
            "trace_ref": ledger.trace_ref,
            "checkpoint_receipt": self._clean_receipt(ledger.task.session_id),
            "session_id": ledger.task.session_id,
        }

    def _cancelled_outcome(
        self, run_id: str, ledger: RunLedger
    ) -> tuple[RunStatus, str]:
        if run_id not in self.cancel_requested:
            return RunStatus.RECOVERY_REQUIRED, "execution_cancelled"
        if ledger.open_tools > 0:
            return RunStatus.RECOVERY_REQUIRED, "cancelled_with_unknown_effect"
        if ledger.task.session_id is not None:
            try:
                report = self.execution.store.inspect(ledger.task.session_id)
            except Exception:
                return RunStatus.RECOVERY_REQUIRED, "cancelled_cleanup_unknown"
            if report.status != "clean" or report.unresolved is not None:
                return RunStatus.RECOVERY_REQUIRED, "cancelled_cleanup_requires_review"
        return RunStatus.CANCELLED, "cancelled"

    async def cancel(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status is RunStatus.QUEUED:
            self.queued_ids.discard(run_id)
            self.store.update_run(
                run_id, status=RunStatus.CANCELLED, error="cancelled", ended_at=_now()
            )
        elif run.status is RunStatus.RUNNING and self.active_run_id == run_id:
            self.cancel_requested.add(run_id)
            if self.active_task is not None:
                self.active_task.cancel()
                await settle_owned(self.active_task)
        else:
            raise ValueError("Run is not cancellable in its current state")
        updated = self.get_run(run_id)
        assert updated is not None
        return updated

    def recovery(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status is not RunStatus.RECOVERY_REQUIRED:
            raise ValueError("Run does not require recovery")
        if run.session_id is None:
            raise ValueError("Run has no Session checkpoint to inspect")
        task = self._runtime_task(run)
        review = self.execution.recovery(task)
        return {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "expected_cursor": self.cursor(run_id),
            **review,
        }

    def reconcile(self, run_id: str, review: dict[str, Any]) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status is not RunStatus.RECOVERY_REQUIRED or run.session_id is None:
            raise ValueError("Run does not require recovery")
        task = self._runtime_task(run)
        self.execution.reconcile(task, review)
        metadata = {
            **run.metadata,
            "recovery_note": review.get("note"),
            "recovery_action": review.get("action"),
        }
        return self.store.update_run(
            run_id,
            status=RunStatus.PAUSED,
            error="reconciled",
            metadata=metadata,
        )

    def preview_snapshot(self, run_id: str) -> str:
        ledger = self.active_ledgers.get(run_id)
        return ledger.visible_text if ledger is not None else ""

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

    async def _work(self) -> None:
        while True:
            run_id = await self.queue.get()
            self.queued_ids.discard(run_id)
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

    def _handoff_development(self, run: Run, ledger: RunLedger) -> None:
        prompt = ledger.task.development_prompt or ledger.task.prompt
        existing = next(
            (
                task
                for task in self.store.list_tasks()
                if task.conversation_id == run.conversation_id
                and task.metadata.get("handoff_source_run") == run.run_id
            ),
            None,
        )
        if existing is not None:
            self.store.update_run(
                run.run_id,
                status=RunStatus.COMPLETED,
                result="development handoff already queued",
                task_id=existing.task_id,
                **_evidence_for_update(self._evidence(ledger)),
            )
            return
        task_id: str | None = None
        if self.legacy_service is not None:
            try:
                source = run.source or ConversationSource.CLI
                legacy = self.legacy_service.submit(
                    prompt,
                    source.value,
                    request_id="handoff:" + run.run_id,
                    mode="development",
                    conversation_key=run.conversation_id,
                )
                origins = list(ledger.task.history_origins)
                if ledger.task.session_id is not None:
                    report = self.execution.store.inspect(ledger.task.session_id)
                    if report.status == "clean":
                        origins.append(report.receipt)
                if origins:
                    self.legacy_service.record(
                        legacy.model_copy(update={"history_origins": tuple(origins)}),
                        "handoff_history",
                    )
                task_id = legacy.task_id
            except Exception:
                task_id = None
        if task_id is None:
            pending = self.store.create_task(
                conversation_id=run.conversation_id,
                task_id=new_task_id(),
                title=prompt[:300],
                goal=prompt,
                status=TaskStatus.TODO,
                metadata={"handoff_source_run": run.run_id, "pending": True},
            )
            self.store.update_run(
                run.run_id,
                status=RunStatus.PAUSED,
                error="development_handoff_pending",
                task_id=pending.task_id,
                **_evidence_for_update(self._evidence(ledger)),
            )
            return
        self.store.create_task(
            conversation_id=run.conversation_id,
            task_id=task_id,
            title=prompt[:300],
            goal=prompt,
            status=TaskStatus.IN_PROGRESS,
            metadata={"handoff_source_run": run.run_id},
        )
        self.store.update_run(
            run.run_id,
            status=RunStatus.COMPLETED,
            result="development handoff queued",
            task_id=task_id,
            **_evidence_for_update(self._evidence(ledger)),
        )

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
            on_event=lambda _kind, persisted: self.changed.set() if persisted else None,
        )
        self.active_ledgers[run_id] = ledger

        def emit(kind: str, data: dict[str, Any]) -> None:
            current = ledger.inspect(run_id)
            ledger.record(current, kind, data)
            if kind == "model_text_delta":
                self.publish(run_id, {"type": "preview", "text": data["text"]})
            else:
                self.publish(run_id, {"type": "event", "kind": kind, "data": data})

        try:
            result = await self.execution(ledger.task, emit, service=ledger)
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
                result=result,
                **_evidence_for_update(self._evidence(ledger)),
                ended_at=_now(),
            )
        except TaskPaused as exc:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.PAUSED,
                error=str(exc),
                **_evidence_for_update(self._evidence(ledger)),
                ended_at=_now(),
            )
        except TaskNeedsRecovery:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="effects_require_host_review",
                **_evidence_for_update(self._evidence(ledger)),
                ended_at=_now(),
            )
        except TaskAdvanced:
            ledger.flush_preview()
            try:
                self._handoff_development(run, ledger)
            except Exception:
                pending = self.store.create_task(
                    conversation_id=run.conversation_id,
                    task_id=new_task_id(),
                    title=(ledger.task.development_prompt or ledger.task.prompt)[:300],
                    goal=ledger.task.development_prompt or ledger.task.prompt,
                    status=TaskStatus.TODO,
                    metadata={
                        "handoff_source_run": run.run_id,
                        "pending": True,
                    },
                )
                self.store.update_run(
                    run_id,
                    status=RunStatus.PAUSED,
                    error="development_handoff_pending",
                    task_id=pending.task_id,
                    **_evidence_for_update(self._evidence(ledger)),
                    ended_at=_now(),
                )
        except asyncio.CancelledError:
            ledger.flush_preview()
            status, error = self._cancelled_outcome(run_id, ledger)
            self.store.update_run(
                run_id,
                status=status,
                error=error,
                **_evidence_for_update(self._evidence(ledger)),
                ended_at=_now(),
            )
            raise
        except Exception as exc:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.FAILED,
                error=type(exc).__name__,
                **_evidence_for_update(self._evidence(ledger)),
                ended_at=_now(),
            )
        finally:
            self.active_ledgers.pop(run_id, None)
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


def _evidence_for_update(evidence: dict[str, Any]) -> dict[str, Any]:
    """Filter optional Run evidence for keyword-style update calls."""
    return {
        "budget": evidence.get("budget") or {},
        "trace_ref": evidence.get("trace_ref"),
        "checkpoint_receipt": evidence.get("checkpoint_receipt"),
        "session_id": evidence.get("session_id"),
    }


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "ConversationBusy",
    "ConversationRecoveryRequired",
    "ConversationService",
    "RunLedger",
]
