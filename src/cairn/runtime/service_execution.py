"""Runtime-owned worktrees and durable Agent turns shared by all entry adapters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.llm import LLMClient
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    SessionPermissionHandler,
)
from cairn.core.tasks import TaskSpec, TaskStatus
from cairn.infra.git.worktree import WorktreeProvider
from cairn.infra.runtime_catalog import RuntimeTask
from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import HostResolution, SessionBinding
from cairn.infra.sessions.store import SessionStore
from cairn.infra.workspace.lease import WorkspaceLease
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.task_service import TaskService
from cairn.runtime.tasks.runner import CodingTaskRunner


class ServiceExecution:
    def __init__(
        self,
        source: Path,
        state: Path,
        worktrees: Path,
        llm_factory: Callable[[], LLMClient],
        *,
        budget: RunBudget,
        allow_shell: bool = False,
    ) -> None:
        self.source = Workspace(source)
        self.state = state
        self.worktrees = worktrees.resolve()
        if self.worktrees == self.source.root or self.worktrees.is_relative_to(
            self.source.root
        ):
            raise ValueError("Worktrees must live outside source")
        self.provider = WorktreeProvider(self.source, self.worktrees)
        self.store = SessionStore(
            state / "sessions", forbidden_roots=(self.source.root, self.worktrees)
        )
        self.llm_factory = llm_factory
        self.budget = budget
        self.allow_shell = allow_shell
        self.service: TaskService | None = None

    async def __call__(
        self, task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
    ) -> str:
        assert self.service is not None
        if task.worktree is None:
            handle = await self.provider.create("HEAD", "cairn/runtime/" + task.task_id)
            handle.retain()
            task = self.service.record(
                task.model_copy(
                    update={
                        "worktree": str(handle.path),
                        "branch": handle.branch,
                        "session_id": str(uuid4()),
                    }
                ),
                "worktree_ready",
                {"path": str(handle.path), "branch": handle.branch},
            )
        assert task.worktree is not None and task.session_id is not None
        path = Path(task.worktree)
        if path.resolve() != path or not path.is_relative_to(self.worktrees):
            raise ValueError("Stored worktree identity is invalid")
        session_id = task.session_id
        workspace = Workspace(path)
        lease = WorkspaceLease(self.state / "leases" / (task.task_id + ".lock"))
        if not lease.acquire():
            raise RuntimeError("Task worktree is owned by another executor")
        sessions: list[DurableSession] = []

        def bind(agent: Agent) -> DurableSession:
            def binding() -> SessionBinding:
                return SessionBinding(
                    session_id=session_id,
                    workspace=observe_workspace(workspace, frozenset()),
                    provider=None,
                    observed_at_utc=datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                )

            current = binding()
            if task.attempts == 1:
                session = DurableSession.create(
                    self.store,
                    agent=agent,
                    binding=current,
                    observe_binding=binding,
                    owns_store=False,
                )
            else:
                session = DurableSession.reopen(
                    self.store,
                    session_id,
                    agent=agent,
                    binding=current,
                    observe_binding=binding,
                    owns_store=False,
                )
            if task.attempts > 1:
                report = session.inspect()
                if report.status != "clean":
                    session.close()
                    raise RuntimeError(
                        "Original task requires host reconciliation; effects cannot be replayed"
                    )
                # Explicit continue authorizes a fresh binding only for a clean,
                # committed checkpoint. Never abandon an unfinished turn here.
                fresh = binding()
                session.reconcile(
                    report,
                    host_resolution=HostResolution(
                        session_id=session_id,
                        turn_id=None,
                        actor="runtime-continue",
                        resolved_at_utc=datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        binding=fresh,
                    ),
                )
            sessions.append(session)
            return session

        def permissions() -> SessionPermissionHandler:
            handler = SessionPermissionHandler(
                prompt=lambda request: (
                    PermissionChoice.ALLOW_SESSION
                    if self.allow_shell
                    and request.capability is PermissionCapability.LOCAL_SHELL
                    else PermissionChoice.DENY
                ),
            )

            handler.grants.add(PermissionCapability.WORKSPACE_TRUST)
            return handler

        def observe(event: Event) -> None:
            # Arguments and output bodies stay in the protected checkpoint, not
            # in a second unfiltered public trace stream.
            if event.type in {"model_text_delta", "model_reasoning_delta"}:
                return
            allowed = {
                "step",
                "max_steps",
                "tool",
                "tool_call_id",
                "error_type",
                "status",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "reason",
                "limit",
                "used",
            }
            data = {key: value for key, value in event.data.items() if key in allowed}
            emit(event.type, data)

        runner = CodingTaskRunner(
            workspace=workspace,
            llm=self.llm_factory(),
            budget=self.budget,
            permission_handler=permissions(),
            event_handler=observe,
            durable_session_factory=bind,
            conversation_only=True,
            sandbox_shell=self.allow_shell,
            system_prompt="You are Cairn's development Agent. Work in this task's workspace, use tools when needed, preserve evidence and report results.",
        )
        try:
            result = await runner.run(
                TaskSpec(
                    task_id=task.task_id,
                    prompt=task.prompt
                    if task.attempts == 1
                    else "Continue this same task from the committed conversation and existing worktree. Do not repeat completed tool effects. Original goal: "
                    + task.prompt,
                )
            )
            summary = runner.last_summary
            self.service.record(
                self.service.inspect(task.task_id).model_copy(
                    update={
                        "metrics": summary.model_dump(mode="json")
                        if summary is not None
                        else {"attempt": task.attempts}
                    }
                ),
                "attempt_settled",
            )
            if result.status is TaskStatus.BUDGET_EXHAUSTED:
                from cairn.runtime.task_service import TaskPaused

                raise TaskPaused(
                    result.final_response
                    or "Task budget reached; committed history and worktree retained"
                )
            if result.status is not TaskStatus.COMPLETED:
                raise RuntimeError(result.error or result.status.value)
            return result.final_response or ""
        finally:
            for session in sessions:
                session.close()
            lease.release()

    def history(self, task: RuntimeTask) -> list[dict[str, Any]]:
        if task.session_id is None:
            return []
        report = self.store.inspect(task.session_id)
        from cairn.infra.sessions.codec import restore_messages

        return [
            message.model_dump(mode="json")
            for message in restore_messages(report.checkpoint)
        ]

    def close(self) -> None:
        self.store.close()
