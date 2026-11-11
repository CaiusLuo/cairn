"""Online Conversation/Run execution for ordinary chat.

DomainStore is the write authority for conversation and run facts. RuntimeTask
is used only as a transient adapter for the existing LangGraph execution loop;
it is never written to RuntimeCatalogue on this path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from cairn.agent.session.models import CheckpointReceipt
from cairn.conversation.models import (
    Conversation,
    ConversationSource,
    Run,
    RunEvent,
    RunStatus,
    utc_now,
    workspace_id_for,
)
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.handoff import DevelopmentHandoff
from cairn.development.models import HandoffStatus
from cairn.development.service import (
    TaskAdvanced,
    TaskNeedsRecovery,
    TaskPaused,
    TaskService,
)
from cairn.shared.lifecycle import settle_owned

LOGGER = logging.getLogger(__name__)


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
        on_session_bound: Callable[[str], None] | None = None,
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
            model_id=run.metadata.get("model_id"),
            session_id=session_id,
            history_origins=history_origins,
        )
        self.preview = ""
        self.visible_text = ""
        self.trace_ref: str | None = None
        self.open_tools = 0
        self._session_id = session_id
        self._on_event = on_event
        self._on_session_bound = on_session_bound

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def bind_session(self, session_id: str) -> None:
        """Publish the first reliable Session identity to the host callback."""
        if self._session_id == session_id:
            return
        if self._session_id is not None:
            raise ValueError("Run Session identity changed during execution")
        self._session_id = session_id
        if self._on_session_bound is not None:
            self._on_session_bound(session_id)

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
        legacy_service: TaskService | None = None,
    ) -> None:
        self.store = store
        self.execution = execution
        self.development_handoff = DevelopmentHandoff(store, execution, legacy_service)
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
        self.settled_listeners: list[Callable[[Run], None]] = []

    def add_run_settled_listener(self, listener: Callable[[Run], None]) -> None:
        self.settled_listeners.append(listener)

    def remove_run_settled_listener(self, listener: Callable[[Run], None]) -> None:
        with suppress(ValueError):
            self.settled_listeners.remove(listener)

    async def start(self) -> None:
        if self.worker is not None:
            raise RuntimeError("Conversation service already started")
        # Resume durable handoff intents before deciding whether a Run was
        # interrupted. This completes the Task/Run linkage without replaying
        # an existing RuntimeTask.
        self.development_handoff.recover_incomplete()
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

    def create_development(
        self,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an explicit Development Task through the shared handoff path.

        This is an admission API, not a chat turn: it never calls the model.
        Conversation, Run, Handoff intent, RuntimeTask and Domain Task share
        one stable request identity and can be retried safely.
        """
        if not request_id:
            raise ValueError("Development request_id is required")
        if conversation_id is None:
            conversation = self.store.create_conversation(
                workspace=self.workspace,
                source=source,
                initial_prompt=prompt,
                request_id="development-conv:" + request_id,
            )
        else:
            existing_conversation = self.get_conversation(conversation_id)
            if existing_conversation is None:
                raise ValueError("Conversation not found")
            conversation = existing_conversation
        run_request_id = "development:" + request_id
        run = self.store.accept_run(
            conversation_id=conversation.conversation_id,
            prompt=prompt,
            source=source,
            request_id=run_request_id,
            mode="development",
            status=RunStatus.RUNNING,
        )
        if run.task_id is not None:
            handoff = self.store.get_handoff(run.run_id)
            if handoff is not None and handoff.status is HandoffStatus.COMPLETED:
                return self._development_entry_payload(conversation, run, handoff)
        if run.status is not RunStatus.RUNNING:
            self.store.update_run(run.run_id, status=RunStatus.RUNNING, error="")
            run = self.store.get_run(run.run_id) or run

        origins = self._development_history_origins(
            conversation.conversation_id, exclude_run_id=run.run_id
        )
        execution = RuntimeTask(
            task_id=run.run_id,
            prompt=prompt,
            source=source.value,
            mode="development",
            conversation_key=conversation.conversation_id,
            input_prompt=prompt,
            history_origins=origins,
        )

        def evidence() -> dict[str, Any]:
            current = self.store.get_run(run.run_id) or run
            return {
                "budget": dict(current.budget),
                "trace_ref": current.trace_ref,
                "checkpoint_receipt": current.checkpoint_receipt,
                "session_id": current.session_id,
            }

        try:
            self.development_handoff.complete(run, execution, evidence)
        except Exception:
            handoff = self.store.get_handoff(run.run_id)
            if handoff is not None and handoff.status is HandoffStatus.COMPLETED:
                current = self.store.get_run(run.run_id) or run
                return self._development_entry_payload(conversation, current, handoff)
            self.store.update_run(
                run.run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="development_entry_failed",
                ended_at=utc_now(),
            )
            raise
        resolved = self.store.get_run(run.run_id)
        if resolved is None:
            raise RuntimeError("Development Run disappeared during handoff")
        handoff = self.store.get_handoff(run.run_id)
        if handoff is None or handoff.status is not HandoffStatus.COMPLETED:
            raise RuntimeError("Development Handoff did not complete")
        return self._development_entry_payload(conversation, resolved, handoff)

    def _development_history_origins(
        self, conversation_id: str, *, exclude_run_id: str
    ) -> tuple[CheckpointReceipt, ...]:
        for candidate in reversed(
            self.store.list_runs_for_conversation(conversation_id)
        ):
            if candidate.run_id == exclude_run_id:
                continue
            if candidate.checkpoint_receipt is None:
                continue
            try:
                return (CheckpointReceipt.model_validate(candidate.checkpoint_receipt),)
            except ValueError:
                continue
        return ()

    def _development_entry_payload(
        self, conversation: Conversation, run: Run, handoff: Any
    ) -> dict[str, Any]:
        task_id = run.task_id or handoff.domain_task_id
        domain_task = self.store.get_task(task_id) if task_id is not None else None
        runtime_task = None
        tasks = self.development_handoff.tasks
        if task_id is not None and tasks is not None:
            with suppress(KeyError):
                runtime_task = tasks.inspect(task_id)
        task_payload = (
            runtime_task.model_dump(mode="json")
            if runtime_task is not None
            else domain_task.model_dump(mode="json")
            if domain_task is not None
            else None
        )
        return {
            "conversation_id": conversation.conversation_id,
            "run_id": run.run_id,
            "task_id": task_id,
            "status": run.status.value,
            "task": task_payload,
            "run": run.model_dump(mode="json"),
            "handoff": handoff.model_dump(mode="json")
            if hasattr(handoff, "model_dump")
            else handoff,
        }

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

    def get_wechat_conversation(
        self, account_id: str, sender_id: str
    ) -> Conversation | None:
        conversation = self.store.get_wechat_conversation(
            workspace=self.workspace,
            account_id=account_id,
            sender_id=sender_id,
        )
        if conversation is not None and conversation.workspace_id != self.workspace_id:
            return None
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

    def run_by_request(self, conversation_id: str, request_id: str) -> Run | None:
        if self.get_conversation(conversation_id) is None:
            return None
        return self.store.get_run_by_request(conversation_id, request_id)

    def recent_runs(self) -> tuple[Run, ...]:
        return self.store.recent_runs(self.workspace_id)

    def submit(
        self,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        *,
        model_id: str | None = None,
    ) -> Run:
        if self.stopping or self.worker is None or self.worker.done():
            raise RuntimeError("Conversation runtime is not accepting runs")
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        existing = self.store.get_run_by_request(conversation_id, request_id)
        if existing is None:
            if model_id is not None:
                profile = self.execution.provider_runtime
                if (
                    profile is None
                    or model_id not in profile.model_manager.current_model().model_ids
                ):
                    raise ValueError("Model is outside the approved group")
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
            model_id=model_id,
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
        # Only a Session accepted by SessionStore is a publishable identity.
        # The transient task may hold a UUID before DurableSession.create fails.
        session_id = ledger.session_id
        return {
            "budget": dict(ledger.task.metrics),
            "trace_ref": ledger.trace_ref,
            "checkpoint_receipt": self._clean_receipt(session_id),
            "session_id": session_id,
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
                run_id,
                status=RunStatus.CANCELLED,
                error="cancelled",
                ended_at=utc_now(),
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

    def handoff_report(self, run_id: str) -> dict[str, Any]:
        return self.development_handoff.report(run_id)

    async def review_handoff(
        self,
        run_id: str,
        *,
        request_id: str,
        action: str,
        expected_updated_at: str,
        note: str,
    ) -> dict[str, Any]:
        return await self.development_handoff.review(
            run_id,
            request_id=request_id,
            action=action,
            expected_updated_at=expected_updated_at,
            note=note,
        )

    def recovery(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status is not RunStatus.RECOVERY_REQUIRED:
            raise ValueError("Run does not require recovery")
        if run.session_id is None:
            # A crash can happen after the run row is accepted but before the
            # SessionStore row exists. There is no checkpoint to inspect, so
            # expose an explicit, auditable abandon path instead of raising and
            # leaving the Conversation permanently blocked.
            return {
                "run_id": run.run_id,
                "session_id": None,
                "expected_cursor": self.cursor(run_id),
                "status": "missing_session",
                "action": "abandon_without_replay",
                "revision": None,
                "checkpoint_outcome": "missing_session",
                "unresolved_turn_id": None,
                "worktree": None,
                "fences": [],
                "diagnostic": run.error or "session_not_recorded",
                "missing_session": True,
            }
        task = self._runtime_task(run)
        try:
            review = self.execution.recovery(task)
        except Exception as exc:
            # Preserve a bounded diagnostic on the Run so a failed inspection
            # remains auditable instead of becoming an unexplained dead end.
            with suppress(Exception):
                self.store.update_run(
                    run_id,
                    metadata={
                        **run.metadata,
                        "recovery_diagnostic": type(exc).__name__,
                    },
                )
            raise
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
        if run.status is not RunStatus.RECOVERY_REQUIRED:
            raise ValueError("Run does not require recovery")
        if run.session_id is None:
            if (
                review.get("action") != "abandon_without_replay"
                or review.get("reviewed_effects") is not True
            ):
                raise ValueError(
                    "Missing-Session Run requires an explicit abandon_without_replay review"
                )
            note = review.get("note")
            if (
                not isinstance(note, str)
                or not note.strip()
                or any(secret in note for secret in self.execution.sensitive_values)
            ):
                raise ValueError(
                    "Describe the reviewed surviving effects without secrets"
                )
            diagnostic = run.error or "session_not_recorded"
            metadata = {
                **run.metadata,
                "recovery_note": note,
                "recovery_action": "abandon_without_replay",
                "recovery_diagnostic": diagnostic,
            }
            return self.store.update_run(
                run_id,
                status=RunStatus.CANCELLED,
                error="recovery_abandoned_without_session",
                metadata=metadata,
                ended_at=utc_now(),
            )
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

    def _record_handoff_failure(
        self, run_id: str, ledger: RunLedger, exc: Exception
    ) -> None:
        handoff = self.store.get_handoff(run_id)
        if handoff is not None and handoff.status is HandoffStatus.COMPLETED:
            self.store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
                result="development handoff queued",
                task_id=handoff.domain_task_id,
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
            return
        error = (
            "development_handoff_requires_review"
            if handoff is not None
            else "development_handoff_failed"
        )
        self.store.update_run(
            run_id,
            status=RunStatus.RECOVERY_REQUIRED,
            error=error,
            **self._evidence(ledger),
            ended_at=utc_now(),
        )

    async def _work(self) -> None:
        while True:
            run_id = await self.queue.get()
            self.queued_ids.discard(run_id)
            task = asyncio.create_task(self._run(run_id))
            self.active_task = task
            try:
                await task
            except asyncio.CancelledError:
                if self.stopping:
                    raise
            except Exception:
                # _run owns normal failure persistence. This is a last-resort
                # worker boundary so one malformed Run cannot kill the queue.
                LOGGER.exception(
                    "conversation worker swallowed an unhandled run failure"
                )
                with suppress(Exception):
                    current = self.store.get_run(run_id)
                    if current is not None and current.status in {
                        RunStatus.QUEUED,
                        RunStatus.RUNNING,
                    }:
                        self.store.update_run(
                            run_id,
                            status=RunStatus.FAILED,
                            error="conversation_worker_error",
                            ended_at=utc_now(),
                        )
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

        def bind_run_session(session_id: str) -> None:
            self.store.bind_run_session(run_id, session_id)

        ledger = RunLedger(
            self.store,
            run,
            history_origins=origins,
            session_id=run.session_id or (prior.session_id if prior else None),
            on_event=lambda _kind, persisted: self.changed.set() if persisted else None,
            on_session_bound=bind_run_session,
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
            # A prior Session is already a reliable identity; publish it before
            # reopening so a crash while reopening still leaves an auditable link.
            if ledger.session_id is not None:
                bind_run_session(ledger.session_id)
            result = await self.execution(ledger.task, emit, service=ledger)
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.COMPLETED,
                result=result,
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
        except TaskPaused as exc:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.PAUSED,
                error=str(exc),
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
        except TaskNeedsRecovery:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="effects_require_host_review",
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
        except TaskAdvanced:
            ledger.flush_preview()
            try:
                self.development_handoff.complete(
                    run, ledger.task, lambda: self._evidence(ledger)
                )
            except Exception as exc:
                LOGGER.exception("development handoff failed for run %s", run_id)
                self._record_handoff_failure(run_id, ledger, exc)
        except asyncio.CancelledError:
            ledger.flush_preview()
            status, error = self._cancelled_outcome(run_id, ledger)
            self.store.update_run(
                run_id,
                status=status,
                error=error,
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
            raise
        except Exception as exc:
            ledger.flush_preview()
            self.store.update_run(
                run_id,
                status=RunStatus.FAILED,
                error=type(exc).__name__,
                **self._evidence(ledger),
                ended_at=utc_now(),
            )
        finally:
            self.active_ledgers.pop(run_id, None)
            self.cancel_requested.discard(run_id)
            self.active_run_id = None
            self.changed.set()
            self.publish(run_id, {"type": "settled"})
            settled = self.get_run(run_id)
            if settled is not None:
                for listener in tuple(self.settled_listeners):
                    with suppress(Exception):
                        listener(settled)

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


__all__ = [
    "ConversationBusy",
    "ConversationRecoveryRequired",
    "ConversationService",
    "RunLedger",
]
