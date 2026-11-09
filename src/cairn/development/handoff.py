"""Project a committed conversation's development request into the task path."""

from collections.abc import Callable
from typing import Any

from cairn.conversation.models import ConversationSource, Run, RunStatus, utc_now
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.models import TaskStatus
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
        prompt = execution.development_prompt or execution.prompt
        try:
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
                    **evidence(),
                )
                return
            task_id = self._submit(run, execution, prompt)
            if task_id is None:
                self._pending(run, prompt, evidence)
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
                **evidence(),
            )
        except Exception:
            self._pending(run, prompt, evidence, ended_at=utc_now())

    def _submit(self, run: Run, execution: RuntimeTask, prompt: str) -> str | None:
        if self.tasks is None:
            return None
        try:
            source = run.source or ConversationSource.CLI
            task = self.tasks.submit(
                prompt,
                source.value,
                request_id="handoff:" + run.run_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
            origins = list(execution.history_origins)
            if execution.session_id is not None:
                report = self.execution.store.inspect(execution.session_id)
                if report.status == "clean":
                    origins.append(report.receipt)
            if origins:
                self.tasks.record(
                    task.model_copy(update={"history_origins": tuple(origins)}),
                    "handoff_history",
                )
            return task.task_id
        except Exception:
            return None

    def _pending(
        self,
        run: Run,
        prompt: str,
        evidence: Callable[[], dict[str, Any]],
        *,
        ended_at: str | None = None,
    ) -> None:
        pending = self.store.create_task(
            conversation_id=run.conversation_id,
            title=prompt[:300],
            goal=prompt,
            metadata={"handoff_source_run": run.run_id, "pending": True},
        )
        self.store.update_run(
            run.run_id,
            status=RunStatus.PAUSED,
            error="development_handoff_pending",
            task_id=pending.task_id,
            **evidence(),
            ended_at=ended_at,
        )
