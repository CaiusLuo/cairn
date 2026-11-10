"""Durable Conversation Run -> Development Task handoff.

The DomainStore handoff row is the stable join key across the SQLite
Conversation/Task store and the RuntimeCatalogue JSON task store.  Each step is
idempotent and a failed step is resumed from the persisted stage; no synthetic
pending Domain Task is created for an uncertain external write.
"""

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from cairn.conversation.models import ConversationSource, Run, RunStatus, utc_now
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.models import HandoffRecord, HandoffStatus, TaskStatus
from cairn.development.service import TaskService


class DevelopmentHandoff:
    def __init__(
        self,
        store: DomainStore,
        execution: ServiceExecution,
        tasks: TaskService | None,
    ) -> None:
        self.store = store
        self.execution = execution
        self.tasks = tasks

    def complete(
        self,
        run: Run,
        execution: RuntimeTask,
        evidence: Callable[[], dict[str, Any]],
    ) -> None:
        source = run.source or ConversationSource.CLI
        prompt = execution.development_prompt or execution.prompt
        handoff = self.store.begin_handoff(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            request_id="handoff:" + run.run_id,
            prompt=prompt,
            source=source.value,
        )
        if handoff.status is HandoffStatus.COMPLETED:
            self._ensure_completed_run(run, handoff)
            return
        self._advance(
            run,
            handoff,
            execution=execution,
            evidence=evidence,
        )

    def recover_incomplete(self) -> tuple[HandoffRecord, ...]:
        """Resume durable handoffs left behind by a crash or failed step."""
        recovered: list[HandoffRecord] = []
        for handoff in self.store.list_incomplete_handoffs():
            if handoff.status is HandoffStatus.NEEDS_REVIEW:
                continue
            run = self.store.get_run(handoff.run_id)
            if run is None:
                self._mark_needs_review(handoff, "run_missing")
                continue

            def evidence_for_run(run: Run = run) -> dict[str, Any]:
                return self._run_evidence(run)

            try:
                recovered.append(
                    self._advance(
                        run,
                        handoff,
                        execution=None,
                        evidence=evidence_for_run,
                    )
                )
            except Exception as exc:
                self._record_diagnostic(handoff, exc)
        return tuple(recovered)

    def _advance(
        self,
        run: Run,
        handoff: HandoffRecord,
        *,
        execution: RuntimeTask | None,
        evidence: Callable[[], dict[str, Any]],
    ) -> HandoffRecord:
        handoff = self._ensure_runtime_task(run, handoff, execution)
        handoff, domain_task_id = self._ensure_domain_task(run, handoff)
        linked = self.store.associate_run_task(run.run_id, domain_task_id)
        handoff = self.store.update_handoff(
            run.run_id,
            status=HandoffStatus.RUN_LINKED,
        )
        current = self.store.get_run(linked.run_id) or linked
        if (
            current.status is not RunStatus.COMPLETED
            or current.task_id != domain_task_id
        ):
            self.store.update_run(
                run.run_id,
                status=RunStatus.COMPLETED,
                result="development handoff queued",
                task_id=domain_task_id,
                **evidence(),
                ended_at=utc_now(),
            )
        return self.store.update_handoff(
            run.run_id,
            status=HandoffStatus.COMPLETED,
        )

    def _ensure_runtime_task(
        self,
        run: Run,
        handoff: HandoffRecord,
        execution: RuntimeTask | None,
    ) -> HandoffRecord:
        if handoff.runtime_task_id is not None:
            if self.tasks is None:
                raise RuntimeError("Development Handoff requires TaskService")
            try:
                self.tasks.inspect(handoff.runtime_task_id)
            except KeyError:
                self._mark_needs_review(handoff, "runtime_task_missing")
                raise
            return handoff
        if self.tasks is None:
            raise RuntimeError("Development Handoff requires TaskService")
        source = ConversationSource(handoff.source)
        try:
            task = self.tasks.submit(
                handoff.prompt,
                source.value,
                request_id=handoff.request_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
        except Exception as exc:
            if isinstance(exc, (ValueError, KeyError)):
                self._mark_needs_review(handoff, type(exc).__name__)
            else:
                self._record_diagnostic(handoff, exc)
            raise
        handoff = self.store.update_handoff(
            run.run_id,
            status=HandoffStatus.RUNTIME_CREATED,
            runtime_task_id=task.task_id,
        )
        if execution is not None:
            self._record_history_origins(task, execution)
        return handoff

    def _record_history_origins(
        self, task: RuntimeTask, execution: RuntimeTask
    ) -> None:
        assert self.tasks is not None
        origins = list(execution.history_origins)
        if execution.session_id is not None:
            try:
                report = self.execution.store.inspect(execution.session_id)
            except Exception:
                return
            if report.status == "clean":
                origins.append(report.receipt)
        if not origins:
            return
        self.tasks.record(
            task.model_copy(update={"history_origins": tuple(origins)}),
            "handoff_history",
        )

    def _ensure_domain_task(
        self, run: Run, handoff: HandoffRecord
    ) -> tuple[HandoffRecord, str]:
        if handoff.domain_task_id is not None:
            task = self.store.get_task(handoff.domain_task_id)
            if task is None:
                self._mark_needs_review(handoff, "domain_task_missing")
                raise KeyError(handoff.domain_task_id)
            if task.conversation_id != run.conversation_id:
                self._mark_needs_review(handoff, "domain_task_conversation_mismatch")
                raise ValueError("Domain Task belongs to another Conversation")
            return handoff, task.task_id
        try:
            task = self.store.create_task_if_absent(
                conversation_id=run.conversation_id,
                task_id=self._runtime_task_id(handoff),
                title=handoff.prompt[:300],
                goal=handoff.prompt,
                status=TaskStatus.IN_PROGRESS,
                metadata={
                    "handoff_run_id": run.run_id,
                    "handoff_request_id": handoff.request_id,
                    "handoff_source_run": run.run_id,
                },
            )
        except Exception as exc:
            if isinstance(exc, (ValueError, KeyError)):
                self._mark_needs_review(handoff, type(exc).__name__)
            else:
                self._record_diagnostic(handoff, exc)
            raise
        handoff = self.store.update_handoff(
            run.run_id,
            status=HandoffStatus.DOMAIN_CREATED,
            domain_task_id=task.task_id,
        )
        return handoff, task.task_id

    @staticmethod
    def _runtime_task_id(handoff: HandoffRecord) -> str:
        if handoff.runtime_task_id is None:
            raise ValueError("Handoff has no RuntimeTask identity")
        return handoff.runtime_task_id

    def _ensure_completed_run(self, run: Run, handoff: HandoffRecord) -> None:
        if handoff.domain_task_id is None:
            raise ValueError("Completed handoff has no Domain Task identity")
        current = self.store.get_run(run.run_id)
        if current is None:
            raise KeyError(run.run_id)
        if current.task_id == handoff.domain_task_id:
            return
        self.store.update_run(
            run.run_id,
            status=RunStatus.COMPLETED,
            result="development handoff queued",
            task_id=handoff.domain_task_id,
            budget=dict(current.budget),
            trace_ref=current.trace_ref,
            checkpoint_receipt=current.checkpoint_receipt,
            session_id=current.session_id,
            ended_at=current.ended_at or utc_now(),
        )

    def _run_evidence(self, run: Run) -> dict[str, Any]:
        return {
            "budget": dict(run.budget),
            "trace_ref": run.trace_ref,
            "checkpoint_receipt": run.checkpoint_receipt,
            "session_id": run.session_id,
        }

    def _record_diagnostic(self, handoff: HandoffRecord, exc: Exception) -> None:
        with suppress(Exception):
            self.store.update_handoff(
                handoff.run_id,
                diagnostic=type(exc).__name__,
            )

    def _mark_needs_review(self, handoff: HandoffRecord, diagnostic: str) -> None:
        with suppress(Exception):
            self.store.update_handoff(
                handoff.run_id,
                status=HandoffStatus.NEEDS_REVIEW,
                diagnostic=diagnostic,
            )
