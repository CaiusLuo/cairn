"""Durable Conversation Run -> Development Task handoff.

The DomainStore handoff row is the stable join key across the SQLite
Conversation/Task store and the RuntimeCatalogue JSON task store. Each step is
idempotent and a failed step is resumed from the persisted stage; no synthetic
pending Domain Task is created for an uncertain external write.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.agent.session.models import CheckpointReceipt
from cairn.conversation.models import ConversationSource, Run, RunStatus, utc_now
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.models import HandoffRecord, HandoffStatus, TaskStatus
from cairn.development.service import TaskService


class HandoffReviewConflict(ValueError):
    """The operator reviewed a stale Handoff version."""


class HandoffReviewFailed(HandoffReviewConflict):
    """A review with this request id already failed; do not replay it."""

    def __init__(self, diagnostic: str) -> None:
        self.diagnostic = diagnostic or "handoff_review_failed"
        super().__init__(self.diagnostic)


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
        existing = self.store.get_handoff(run.run_id)
        if existing is not None:
            if existing.status is HandoffStatus.COMPLETED:
                self._ensure_completed_run(run, existing)
                return
            if existing.status is HandoffStatus.NEEDS_REVIEW:
                raise ValueError("Handoff requires explicit review before retrying")
            if existing.status is HandoffStatus.REVIEWING:
                raise ValueError("Handoff review is in progress")
            if existing.status is HandoffStatus.ABANDONED:
                raise ValueError("Handoff was abandoned")
        try:
            origins = self._prepare_history_origins(execution)
        except Exception as exc:
            handoff = self.store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt=prompt,
                source=source.value,
                history_origins=(),
            )
            self._mark_needs_review(handoff, "history_origins_unavailable")
            raise exc
        handoff = self.store.begin_handoff(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            request_id="handoff:" + run.run_id,
            prompt=prompt,
            source=source.value,
            history_origins=tuple(origin.model_dump(mode="json") for origin in origins),
        )
        if handoff.status is HandoffStatus.COMPLETED:
            self._ensure_completed_run(run, handoff)
            return
        if handoff.status is HandoffStatus.NEEDS_REVIEW:
            raise ValueError("Handoff requires explicit review before retrying")
        if handoff.status is HandoffStatus.REVIEWING:
            raise ValueError("Handoff review is in progress")
        if handoff.status is HandoffStatus.ABANDONED:
            raise ValueError("Handoff was abandoned")
        self._advance(
            run,
            handoff,
            execution=execution,
            evidence=evidence,
        )

    def recover_incomplete(self) -> tuple[HandoffRecord, ...]:
        """Resume durable handoffs left behind by a crash or failed step."""
        self.store.recover_interrupted_handoff_reviews()
        recovered: list[HandoffRecord] = []
        for handoff in self.store.list_incomplete_handoffs():
            if handoff.status is HandoffStatus.REVIEWING:
                self._mark_needs_review(handoff, "review_interrupted")
                continue
            if handoff.status in {
                HandoffStatus.NEEDS_REVIEW,
                HandoffStatus.ABANDONED,
            }:
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

    def report(self, run_id: str) -> dict[str, Any]:
        handoff = self.store.get_handoff(run_id)
        if handoff is None:
            raise KeyError(run_id)
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        conversation = self.store.get_conversation(run.conversation_id)
        domain_task = (
            self.store.get_task(handoff.domain_task_id)
            if handoff.domain_task_id is not None
            else None
        )
        runtime_task = None
        if handoff.runtime_task_id is not None and self.tasks is not None:
            with suppress(KeyError):
                runtime_task = self.tasks.inspect(handoff.runtime_task_id)
        actions: list[str] = []
        if handoff.status not in {
            HandoffStatus.COMPLETED,
            HandoffStatus.ABANDONED,
            HandoffStatus.REVIEWING,
        }:
            if (
                handoff.runtime_task_id is not None
                and handoff.domain_task_id is not None
            ):
                actions.append("adopt_existing")
            actions.append("abandon")
        return {
            "handoff": handoff.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "conversation": (
                conversation.model_dump(mode="json")
                if conversation is not None
                else None
            ),
            "domain_task": (
                domain_task.model_dump(mode="json") if domain_task is not None else None
            ),
            "runtime_task": (
                runtime_task.model_dump(mode="json")
                if runtime_task is not None
                else None
            ),
            "actions": actions,
        }

    async def review(
        self,
        run_id: str,
        *,
        request_id: str,
        action: str,
        expected_updated_at: str,
        note: str,
    ) -> dict[str, Any]:
        if action not in {"adopt_existing", "abandon"}:
            raise ValueError("Unsupported handoff review action")
        try:
            handoff, review = self.store.begin_handoff_review(
                run_id,
                request_id=request_id,
                action=action,
                expected_updated_at=expected_updated_at,
                note=note,
            )
        except ValueError as exc:
            raise HandoffReviewConflict(str(exc)) from exc
        if review.status.value == "completed":
            return self.report(run_id)
        if review.status.value == "failed":
            raise HandoffReviewFailed(review.diagnostic or "handoff_review_failed")
        try:
            if action == "adopt_existing":
                await self._adopt_existing(handoff, run_id)
                final_status = HandoffStatus.COMPLETED
            else:
                await self._abandon(handoff, run_id)
                final_status = HandoffStatus.ABANDONED
        except asyncio.CancelledError:
            # The review can be cancelled while TaskService.cancel() is
            # awaiting the active executor. The external outcome is unknown;
            # persist a reviewable state and propagate cancellation.
            with suppress(Exception):
                self.store.fail_handoff_review(
                    run_id, request_id, diagnostic="review_cancelled"
                )
            raise
        except Exception as exc:
            diagnostic = (
                str(exc)
                if isinstance(exc, HandoffReviewConflict)
                else type(exc).__name__
            )
            with suppress(Exception):
                self.store.fail_handoff_review(
                    run_id, request_id, diagnostic=diagnostic
                )
            if isinstance(exc, HandoffReviewConflict):
                raise
            raise HandoffReviewConflict("handoff_review_failed") from exc
        try:
            self.store.finish_handoff_review(
                run_id, request_id, final_status=final_status
            )
        except Exception as exc:
            raise HandoffReviewConflict("handoff_review_commit_failed") from exc
        return self.report(run_id)

    async def _adopt_existing(self, handoff: HandoffRecord, run_id: str) -> None:
        if handoff.runtime_task_id is None or handoff.domain_task_id is None:
            raise ValueError("Handoff has no complete Task identity to adopt")
        if self.tasks is None:
            raise RuntimeError("Development Handoff requires TaskService")
        runtime_task = self.tasks.inspect(handoff.runtime_task_id)
        domain_task = self.store.get_task(handoff.domain_task_id)
        if domain_task is None:
            raise HandoffReviewConflict("Handoff Domain Task no longer exists")
        run = self.store.get_run(run_id)
        if run is None:
            raise HandoffReviewConflict("Handoff Run no longer exists")
        if (
            runtime_task.task_id != domain_task.task_id
            or runtime_task.request_id != handoff.request_id
            or runtime_task.conversation_key != handoff.conversation_id
            or runtime_task.mode != "development"
            or domain_task.conversation_id != run.conversation_id
            or domain_task.workspace_id != run.workspace_id
        ):
            raise HandoffReviewConflict("Handoff Task identity does not match the Run")
        origins = self._load_origins(handoff)
        self._validate_history_origins(origins)
        if tuple(runtime_task.history_origins) != origins:
            raise HandoffReviewConflict(
                "Handoff RuntimeTask history origins are incomplete"
            )
        self.store.associate_run_task(run_id, domain_task.task_id)
        self.store.update_run(
            run_id,
            status=RunStatus.COMPLETED,
            result="development handoff reviewed",
            task_id=domain_task.task_id,
            budget=dict(run.budget),
            trace_ref=run.trace_ref,
            checkpoint_receipt=run.checkpoint_receipt,
            session_id=run.session_id,
            ended_at=run.ended_at or utc_now(),
        )

    async def _abandon(self, handoff: HandoffRecord, run_id: str) -> None:
        run = self.store.get_run(run_id)
        conversation = self.store.get_conversation(run.conversation_id) if run else None
        if (
            run is None
            or conversation is None
            or run.conversation_id != handoff.conversation_id
            or conversation.workspace_id != run.workspace_id
        ):
            raise HandoffReviewConflict("Handoff Run/Conversation identity mismatch")
        if handoff.domain_task_id is not None:
            domain_task = self.store.get_task(handoff.domain_task_id)
            if (
                domain_task is None
                or domain_task.task_id != handoff.domain_task_id
                or domain_task.conversation_id != run.conversation_id
                or domain_task.workspace_id != run.workspace_id
            ):
                raise HandoffReviewConflict("Handoff Domain Task identity mismatch")
        if handoff.runtime_task_id is None:
            self._cancel_run_for_abandon(run_id, run)
            return
        if self.tasks is None:
            raise HandoffReviewConflict("TaskService unavailable for safe abandon")
        try:
            runtime_task = self.tasks.inspect(handoff.runtime_task_id)
        except KeyError as exc:
            raise HandoffReviewConflict("Handoff RuntimeTask is missing") from exc
        if (
            runtime_task.task_id != handoff.runtime_task_id
            or runtime_task.request_id != handoff.request_id
            or runtime_task.conversation_key != handoff.conversation_id
            or runtime_task.mode != "development"
        ):
            raise HandoffReviewConflict("Handoff RuntimeTask identity mismatch")
        if runtime_task.worktree is not None:
            worktrees = getattr(self.execution, "worktrees", None)
            if worktrees is None or not Path(
                runtime_task.worktree
            ).resolve().is_relative_to(worktrees):
                raise HandoffReviewConflict("Handoff RuntimeTask workspace mismatch")
        if runtime_task.status == "recovery_required":
            raise HandoffReviewConflict("Handoff RuntimeTask has unknown effects")
        if runtime_task.status in {"queued", "running"}:
            try:
                settled = await self.tasks.cancel(runtime_task.task_id)
            except Exception as exc:
                raise HandoffReviewConflict(
                    "Handoff RuntimeTask cancellation failed"
                ) from exc
            if (
                settled.task_id != runtime_task.task_id
                or settled.request_id != handoff.request_id
                or settled.conversation_key != handoff.conversation_id
                or settled.status not in {"cancelled", "completed", "paused"}
            ):
                raise HandoffReviewConflict(
                    "Handoff RuntimeTask cancellation is not safely settled"
                )
            runtime_task = settled
        elif runtime_task.status not in {"cancelled", "completed", "paused"}:
            raise HandoffReviewConflict(
                "Handoff RuntimeTask cannot be safely abandoned"
            )
        self._cancel_run_for_abandon(run_id, run)

    def _cancel_run_for_abandon(self, run_id: str, run: Run) -> None:
        if run.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            self.store.update_run(
                run_id,
                status=RunStatus.CANCELLED,
                error="handoff_abandoned",
                ended_at=utc_now(),
            )

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
        try:
            origins = self._load_origins(handoff)
            self._validate_history_origins(origins)
        except Exception as exc:
            self._mark_needs_review(handoff, "history_origins_unavailable")
            raise exc
        if handoff.runtime_task_id is not None:
            if self.tasks is None:
                raise RuntimeError("Development Handoff requires TaskService")
            try:
                task = self.tasks.inspect(handoff.runtime_task_id)
            except KeyError:
                self._mark_needs_review(handoff, "runtime_task_missing")
                raise
            if tuple(task.history_origins) != origins:
                self._mark_needs_review(handoff, "history_origins_mismatch")
                raise ValueError("RuntimeTask history origins are incomplete")
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
                history_origins=origins,
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
        return handoff

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

    def _prepare_history_origins(
        self, execution: RuntimeTask
    ) -> tuple[CheckpointReceipt, ...]:
        origins = {
            origin.revision.session_id: origin for origin in execution.history_origins
        }
        if execution.session_id is not None:
            report = self.execution.store.inspect(execution.session_id)
            if report.status != "clean":
                raise ValueError("checkpoint_not_clean")
            origins[execution.session_id] = report.receipt
        result = tuple(origins.values())
        self._validate_history_origins(result)
        return result

    def _load_origins(self, handoff: HandoffRecord) -> tuple[CheckpointReceipt, ...]:
        try:
            return tuple(
                CheckpointReceipt.model_validate(origin)
                for origin in handoff.history_origins
            )
        except Exception as exc:
            raise ValueError("history_origins_invalid") from exc

    def _validate_history_origins(self, origins: tuple[CheckpointReceipt, ...]) -> None:
        if len(origins) > 8:
            raise ValueError("too_many_history_origins")
        for origin in origins:
            try:
                report = self.execution.store.inspect(origin.revision.session_id)
            except Exception as exc:
                raise ValueError("history_session_unavailable") from exc
            if report.status != "clean":
                raise ValueError("history_session_not_clean")
            if report.revision.session_id != origin.revision.session_id:
                raise ValueError("history_session_mismatch")
            if origin.revision.head_seq > report.revision.head_seq:
                raise ValueError("history_receipt_newer_than_session")
            if report.receipt != origin:
                raise ValueError("history_receipt_mismatch")
            if origin.turn_id is not None and not any(
                attempt.intent.turn_id == origin.turn_id for attempt in report.attempts
            ):
                raise ValueError("history_turn_missing")

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
            current = self.store.get_handoff(handoff.run_id)
            if current is not None and current.status is HandoffStatus.NEEDS_REVIEW:
                return
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
