"""Runtime-owned worktrees and durable Agent turns shared by all entry adapters."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
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
from cairn.infra.credential_paths import protect_host_paths
from cairn.infra.git.worktree import WorktreeProvider
from cairn.infra.redaction import StreamRedactor, redact_text, redact_value
from cairn.infra.runtime_catalog import RuntimeTask
from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import (
    HostResolution,
    ProviderReference,
    Revision,
    SessionBinding,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.workspace.lease import WorkspaceLease
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.provider_runtime import ProviderRuntime
from cairn.runtime.task_service import TaskNeedsRecovery, TaskService
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
        allow_network: bool = False,
        provider_runtime: ProviderRuntime | None = None,
        secret_env_keys: frozenset[str] = frozenset(),
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.source = Workspace(source)
        self.state = state
        self.worktrees = worktrees.resolve()
        if self.worktrees == self.source.root or self.worktrees.is_relative_to(
            self.source.root
        ):
            raise ValueError("Worktrees must live outside source")
        self.sensitive_values = sensitive_values
        self.provider_runtime = provider_runtime
        self.secret_env_keys = secret_env_keys
        self.allow_network = allow_network
        self.provider = WorktreeProvider(
            self.source, self.worktrees, secret_env_keys=secret_env_keys
        )
        self._store: SessionStore | None = None
        self.llm_factory = llm_factory
        self.budget = budget
        self.allow_shell = allow_shell
        self.service: TaskService | None = None

    @property
    def store(self) -> SessionStore:
        # Open on the runtime owner thread, rather than an ASGI factory thread.
        if self._store is None:
            self._store = SessionStore(
                self.state / "sessions",
                forbidden_roots=(self.source.root, self.worktrees),
                sensitive_values=self.sensitive_values,
            )
        return self._store

    async def __call__(
        self, task: RuntimeTask, emit: Callable[[str, dict[str, Any]], None]
    ) -> str:
        started = perf_counter()
        assert self.service is not None
        if task.worktree is None:
            handle = await self.provider.create("HEAD", "cairn/runtime/" + task.task_id)
            try:
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
            except BaseException as primary:
                try:
                    await handle.release()
                except BaseException as failure:
                    primary.add_note(
                        "Worktree admission cleanup failed: " + type(failure).__name__
                    )
                raise
            handle.retain()
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
        message_start = 0
        effects_unknown = False
        stream = StreamRedactor(self.sensitive_values)

        def bind(agent: Agent) -> DurableSession:
            nonlocal message_start
            profile = self.provider_runtime
            reference = None
            if profile is not None:
                group = profile.model_manager.current_model()
                config = profile.provider.config
                reference = ProviderReference(
                    provider=profile.provider.name,
                    model_group=group.name,
                    model_ids=group.model_ids,
                    config_digest=hashlib.sha256(
                        json.dumps(
                            [
                                config.base_url,
                                config.api_key_env,
                                group.name,
                                group.model_ids,
                            ]
                        ).encode()
                    ).hexdigest(),
                    system_prompt_digest=hashlib.sha256(
                        agent.system_prompt.encode()
                    ).hexdigest(),
                    context_budget=agent.context_builder.budget.max_tokens,
                    output_reserve=agent.context_builder.budget.response_tokens,
                    max_steps=self.budget.max_steps,
                )
            if not self.allow_shell:
                agent.tools.unregister_tool("bash")

            def binding() -> SessionBinding:
                return SessionBinding(
                    session_id=session_id,
                    workspace=observe_workspace(workspace, self.secret_env_keys),
                    provider=reference,
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
            message_start = len(agent.state.messages)
            sessions.append(session)
            return session

        def permissions() -> SessionPermissionHandler:
            handler = SessionPermissionHandler(
                prompt=lambda request: (
                    PermissionChoice.ALLOW_SESSION
                    if (
                        self.allow_shell
                        and request.capability is PermissionCapability.LOCAL_SHELL
                    )
                    or (
                        self.allow_network
                        and request.capability is PermissionCapability.NETWORK
                    )
                    else PermissionChoice.DENY
                ),
            )

            handler.grants.add(PermissionCapability.WORKSPACE_TRUST)
            return handler

        def observe(event: Event) -> None:
            nonlocal effects_unknown, stream
            if event.type == "model_stream_start":
                stream = StreamRedactor(self.sensitive_values)
            elif event.type == "model_stream_end":
                if event.data.get("status") == "completed":
                    tail = stream.feed("", final=True)
                    if tail:
                        emit("model_text_delta", {"text": tail})
                stream = StreamRedactor(self.sensitive_values)
            if event.type == "model_text_delta":
                text = stream.feed(str(event.data.get("text", "")))
                if text:
                    emit(event.type, {"text": text})
                return
            if event.type == "model_reasoning_delta":
                return
            allowed = {
                "step",
                "max_steps",
                "attempt",
                "tool",
                "tool_call_id",
                "error_type",
                "failure_kind",
                "status",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "reason",
                "limit",
                "used",
                "budget",
            }
            data = {key: value for key, value in event.data.items() if key in allowed}
            if event.type == "tool_call":
                arguments = redact_value(
                    event.data.get("arguments", {}), self.sensitive_values
                )
                encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                data["arguments_preview"] = encoded[:4096]
                data["arguments_truncated"] = len(encoded) > 4096
                data["arguments_digest"] = hashlib.sha256(encoded.encode()).hexdigest()
            emit(event.type, data)
            if event.type == "tool_error" and event.data.get("failure_kind") in {
                "effect_unknown",
                "deadline",
                "cancelled",
            }:
                effects_unknown = True
                # The original loop will settle its pending calls and preserve
                # executed facts. Stop before another model/tool request can replay.
                raise TaskNeedsRecovery()

        runner = CodingTaskRunner(
            workspace=workspace,
            llm=self.llm_factory(),
            context_builder=self.provider_runtime.context_builder
            if self.provider_runtime is not None
            else None,
            model_executor=self.provider_runtime.model_executor
            if self.provider_runtime is not None
            else None,
            secret_env_keys=self.secret_env_keys,
            provider_name=self.provider_runtime.provider.name
            if self.provider_runtime is not None
            else None,
            budget=self.budget,
            permission_handler=permissions(),
            event_handler=observe,
            durable_session_factory=bind,
            conversation_only=True,
            sandbox_shell=self.allow_shell,
            system_prompt="You are Cairn's development Agent. Work in this task's workspace, use tools when needed, preserve evidence and report results.",
        )

        def record_attempt() -> None:
            assert self.service is not None
            summary = runner.last_summary
            usage = sessions[-1].agent.last_budget_usage if sessions else None
            metrics = dict(task.metrics)
            metrics["live"] = None
            if usage is not None:
                current = asdict(usage)
                metrics["current_turn"] = current
                for name in (
                    "steps",
                    "requests",
                    "tool_attempts",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "estimated_cost",
                ):
                    value = current[name]
                    previous = metrics.get(name, 0)
                    metrics[name] = (
                        previous + value
                        if previous is not None and value is not None
                        else None
                    )
                metrics["stop_reason"] = usage.stop_reason
                metrics["stagnant_steps"] = usage.stagnant_steps
            metrics["elapsed_seconds"] = (
                metrics.get("elapsed_seconds", 0) + perf_counter() - started
            )
            metrics["limits"] = asdict(self.budget)
            metrics["last_attempt"] = (
                summary.model_dump(mode="json") if summary is not None else None
            )
            self.service.record(
                self.service.inspect(task.task_id).model_copy(
                    update={"metrics": metrics}
                ),
                "attempt_settled",
            )

        try:
            private_root = (
                Path(
                    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                )
                / "cairn"
            )
            with protect_host_paths((self.state, private_root)):
                result = await runner.run(
                    TaskSpec(
                        task_id=task.task_id,
                        prompt=task.input_prompt or task.prompt
                        if task.attempts == 1 or task.input_prompt is not None
                        else "Continue this same task from the committed conversation and existing worktree. Do not repeat completed tool effects. Original goal: "
                        + task.prompt
                        + (
                            " Host-reviewed surviving effects: " + task.recovery_note
                            if task.recovery_note
                            else ""
                        ),
                    )
                )
            interrupted_tool = (
                any(
                    message.role == "tool"
                    and message.content is not None
                    and json.loads(message.content).get("type") == "ToolCancelled"
                    for message in sessions[-1].agent.state.messages[message_start:]
                )
                if sessions
                else False
            )
            if (
                effects_unknown
                or interrupted_tool
                or (sessions and sessions[-1].inspect().status != "clean")
            ):
                raise TaskNeedsRecovery()
            if result.status is TaskStatus.BUDGET_EXHAUSTED:
                from cairn.runtime.task_service import TaskPaused

                usage = sessions[-1].agent.last_budget_usage if sessions else None
                observed = []
                for message in (
                    sessions[-1].agent.state.messages[message_start:]
                    if sessions
                    else []
                ):
                    if message.role == "tool" and message.content:
                        value = json.loads(message.content)
                        observed.append(
                            f"{message.tool_call_id}: "
                            + str(
                                value.get("stdout")
                                or value.get("error")
                                or value.get("stderr")
                                or "工具结果已保存"
                            )[:500]
                        )
                phase = f"已暂停: {usage.stop_reason if usage else '任务预算已到'}。本轮 {usage.steps if usage else '未知'} 步, Token {usage.total_tokens if usage and usage.total_tokens is not None else '未知'}。\n原任务、会话与 Worktree 已保留, 目标尚未完成, 可确认后继续。"
                if observed:
                    phase += "\n最近工具结果: \n" + "\n".join(observed[-3:])
                raise TaskPaused(phase)
            if result.status is not TaskStatus.COMPLETED:
                raise RuntimeError(result.error or result.status.value)
            tail = stream.feed("", final=True)
            if tail:
                emit("model_text_delta", {"text": tail})
            return redact_text(result.final_response or "", self.sensitive_values)
        finally:
            with suppress(Exception):
                record_attempt()
            try:
                for session in sessions:
                    session.close()
            finally:
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

    def recovery(self, task: RuntimeTask) -> dict[str, Any]:
        if task.session_id is None or task.worktree is None:
            raise ValueError(
                "Worktree admission was interrupted; host inspection required"
            )
        report = self.store.inspect(task.session_id)
        unresolved = report.unresolved
        return {
            "revision": report.revision.model_dump(mode="json"),
            "action": "abandon_without_replay"
            if unresolved
            else "acknowledge_checkpoint",
            "status": report.status,
            "worktree": task.worktree,
            "checkpoint_outcome": report.checkpoint.outcome,
            "unresolved_turn_id": unresolved.intent.turn_id if unresolved else None,
            "fences": [f.model_dump(mode="json") for f in unresolved.diagnostics.fences]
            if unresolved
            else [],
        }

    def reconcile(self, task: RuntimeTask, review: dict[str, Any]) -> None:
        if (
            task.session_id is None
            or task.worktree is None
            or review.get("reviewed_effects") is not True
        ):
            raise ValueError("Explicit host review required")
        note = review.get("note")
        if (
            not isinstance(note, str)
            or not note.strip()
            or any(s in note for s in self.sensitive_values)
        ):
            raise ValueError("Describe the reviewed surviving effects without secrets")
        expected = Revision.model_validate(review.get("revision"))
        report = self.store.inspect(task.session_id)
        if report.revision != expected or review.get("action") != (
            "abandon_without_replay" if report.unresolved else "acknowledge_checkpoint"
        ):
            raise ValueError("Recovery report is stale")
        path = Path(task.worktree)
        if path.resolve() != path or not path.is_relative_to(self.worktrees):
            raise ValueError("Worktree identity changed")
        lease = WorkspaceLease(self.state / "leases" / (task.task_id + ".lock"))
        if not lease.acquire():
            raise ValueError("Worktree still owned")
        try:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            binding = SessionBinding(
                session_id=task.session_id,
                workspace=observe_workspace(Workspace(path), self.secret_env_keys),
                provider=report.binding.provider,
                observed_at_utc=now,
            )
            self.store.reconcile(
                expected,
                HostResolution(
                    session_id=task.session_id,
                    turn_id=report.unresolved.intent.turn_id
                    if report.unresolved
                    else None,
                    actor="runtime-host-review",
                    resolved_at_utc=now,
                    binding=binding,
                    note=note,
                ),
            )
        finally:
            lease.release()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
