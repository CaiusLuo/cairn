"""Runtime-owned worktrees and durable Agent turns shared by all entry adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from cairn.agent.budget import RunBudget
from cairn.agent.harness import Agent
from cairn.agent.models.client import LLMClient
from cairn.agent.models.executor import ModelExecutor
from cairn.agent.models.runtime import ProviderRuntime
from cairn.agent.models.selection import ModelManager
from cairn.agent.permissions import (
    PermissionCapability,
    PermissionChoice,
    SessionPermissionHandler,
)
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.host import observe_workspace
from cairn.agent.session.models import (
    HostResolution,
    ProviderReference,
    Revision,
    SessionBinding,
)
from cairn.agent.session.store import SessionStore
from cairn.agent.turn.events import Event
from cairn.agent.workspace.lease import WorkspaceLease
from cairn.agent.workspace.workspace import Workspace
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.models import TaskSpec, TaskStatus
from cairn.development.execution.runner import CodingTaskRunner
from cairn.development.request import DevelopmentRequest
from cairn.development.service import (
    TaskAdvanced,
    TaskNeedsRecovery,
    TaskPaused,
    TaskService,
)
from cairn.development.worktree import WorktreeProvider
from cairn.shared.credential_paths import protect_host_paths
from cairn.shared.redaction import StreamRedactor, redact_text, redact_value


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
        self.configure_agent: Callable[[Agent, RuntimeTask], None] | None = None
        self.validate_task: Callable[[RuntimeTask], None] | None = None
        self.protected_paths: tuple[Path, ...] = ()

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

    def protect_secrets(self, values: tuple[str, ...]) -> None:
        self.sensitive_values = tuple(dict.fromkeys((*self.sensitive_values, *values)))
        if self._store is not None:
            self._store.sensitive_values = self.sensitive_values

    def profile_for(self, task: RuntimeTask) -> ProviderRuntime | None:
        profile = self.provider_runtime
        if profile is None or task.model_id is None:
            return profile
        group = profile.model_manager.current_model()
        if task.model_id not in group.model_ids:
            raise ValueError("Model not in the host-approved group")
        selected = replace(
            group,
            model_ids=(
                task.model_id,
                *(ident for ident in group.model_ids if ident != task.model_id),
            ),
        )
        manager = ModelManager(
            replace(profile.provider.config, model_config=(selected,))
        )
        manager.select_model_id(task.model_id)
        llm, context = profile.runtime_factory(task.model_id)
        return replace(
            profile,
            model_manager=manager,
            llm=llm,
            context_builder=context,
            model_executor=ModelExecutor(
                manager, profile.runtime_factory, allow_fallback=profile.allow_fallback
            ),
        )

    async def __call__(
        self,
        task: RuntimeTask,
        emit: Callable[[str, dict[str, Any]], None],
        *,
        service: Any | None = None,
    ) -> str:
        started = perf_counter()
        ledger = service if service is not None else self.service
        assert ledger is not None
        if self.validate_task is not None:
            self.validate_task(task)
        budget = self._remaining_budget(task, ledger)
        profile = self.profile_for(task)
        new_session = False
        development = DevelopmentRequest()
        chatting = (
            task.worktree is None
            and task.mode == "conversation"
            and task.development_prompt is None
        )
        if not chatting and task.worktree is None:
            origins = task.history_origins
            if task.session_id is not None:
                report = self.store.inspect(task.session_id)
                if report.status != "clean":
                    raise TaskNeedsRecovery()
                origins = (*origins, report.receipt)
            handle = await self.provider.create("HEAD", "cairn/runtime/" + task.task_id)
            try:
                task = ledger.record(
                    task.model_copy(
                        update={
                            "worktree": str(handle.path),
                            "branch": handle.branch,
                            "session_id": str(uuid4()),
                            "history_origins": origins,
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
            new_session = True
        elif task.session_id is None:
            task = ledger.record(
                task.model_copy(update={"session_id": str(uuid4())}),
                "conversation_ready",
            )
            new_session = True
        assert task.session_id is not None
        session_id = task.session_id
        workspace = self.source if chatting else Workspace(self._worktree_path(task))
        historical_context = self._historical_context(task)
        lease = WorkspaceLease(self.state / "leases" / (task.task_id + ".lock"))
        if not lease.acquire():
            raise RuntimeError("Task worktree is owned by another executor")
        sessions: list[DurableSession] = []
        message_start = 0
        effects_unknown = False
        stream = StreamRedactor(self.sensitive_values)

        def bind(agent: Agent) -> DurableSession:
            nonlocal message_start
            if self.configure_agent is not None:
                self.configure_agent(agent, task)
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
                    max_steps=budget.max_steps,
                )
            if chatting:
                for schema in agent.tools.schemas():
                    name = schema["function"]["name"]
                    if name not in {
                        "read_file",
                        "glob",
                        "grep",
                        "begin_development",
                        "memory_search",
                        "memory_remember",
                        "memory_forget",
                        "memory_correct",
                        "schedule_create",
                        "schedule_list",
                        "schedule_manage",
                        "read_skill",
                        "read_skill_resource",
                    }:
                        agent.tools.unregister_tool(name)
                agent.after_tools = development.after_tools
            elif not self.allow_shell:
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
            if new_session:
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
            if not new_session:
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
            # RunLedger publishes its durable identity only after the Session
            # store has accepted the Session. TaskService has no such method and
            # keeps its existing RuntimeCatalogue lifecycle unchanged.
            bind_session = getattr(ledger, "bind_session", None)
            if callable(bind_session):
                bind_session(session_id)
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
            llm=profile.llm if profile is not None else self.llm_factory(),
            context_builder=profile.context_builder if profile is not None else None,
            model_executor=profile.model_executor if profile is not None else None,
            secret_env_keys=self.secret_env_keys,
            provider_name=profile.provider.name if profile is not None else None,
            budget=budget,
            extra_tools=(development,) if chatting else (),
            include_repository_context=not chatting,
            personal_context=historical_context,
            permission_handler=permissions(),
            event_handler=observe,
            durable_session_factory=bind,
            conversation_only=True,
            message_id=task.input_id or task.task_id,
            sandbox_shell=self.allow_shell,
            system_prompt=(
                "You are Cairn, one assistant for conversation and project work. Reply directly to ordinary chat. Use tools only when needed; preserve verified facts and never repeat unknown effects. "
                + (
                    "Project files are read-only. To modify the project or execute commands, call begin_development with the user's goal. The host prepares a worktree after this turn; you cannot grant permissions."
                    if chatting
                    else "Work in this task's retained development workspace. Preserve existing changes and report verified results."
                )
            ),
        )

        def record_attempt() -> None:
            assert ledger is not None
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
            window = dict(task.metrics.get("window", {}))
            if usage is not None:
                for name in (
                    "steps",
                    "total_tokens",
                    "estimated_cost",
                    "elapsed_seconds",
                ):
                    previous = window.get(name, 0)
                    value = getattr(usage, name)
                    window[name] = (
                        previous + value
                        if previous is not None and value is not None
                        else None
                    )
            metrics["window"] = window
            metrics["limits"] = asdict(self.budget)
            metrics["last_attempt"] = (
                summary.model_dump(mode="json") if summary is not None else None
            )
            ledger.record(
                ledger.inspect(task.task_id).model_copy(update={"metrics": metrics}),
                "attempt_settled",
            )

        try:
            with protect_host_paths((self.state, *self.protected_paths)):
                result = await runner.run(
                    TaskSpec(
                        task_id=task.task_id,
                        prompt=(
                            task.development_prompt
                            if new_session
                            and not chatting
                            and task.development_prompt is not None
                            else (
                                "Continue from the committed conversation and existing workspace, without repeating completed effects. Most recent user request: "
                                + (
                                    task.input_prompt
                                    or task.development_prompt
                                    or task.prompt
                                )
                                + (
                                    " Host-reviewed surviving effects: "
                                    + task.recovery_note
                                    if task.recovery_note
                                    else ""
                                )
                                if task.resume_requested
                                else task.input_prompt or task.prompt
                            )
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
            if chatting and development.prompt is not None:
                if result.status is not TaskStatus.COMPLETED:
                    raise TaskNeedsRecovery()
                ledger.record(
                    ledger.inspect(task.task_id).model_copy(
                        update={
                            "development_prompt": development.prompt,
                            "input_prompt": development.prompt,
                        }
                    ),
                    "development_requested",
                )
                raise TaskAdvanced()
            if result.status is TaskStatus.BUDGET_EXHAUSTED:
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
                phase = f"已暂停: {(usage.stop_reason or 'context_budget') if usage else '任务预算已到'}。本轮 {usage.steps if usage else '未知'} 步, Token {usage.total_tokens if usage and usage.total_tokens is not None else '未知'}。\n原任务、会话与 Worktree 已保留, 目标尚未完成, 可确认后继续。"
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

    def _worktree_path(self, task: RuntimeTask) -> Path:
        if task.worktree is None:
            raise ValueError("Task has no development worktree")
        path = Path(task.worktree)
        if path.resolve() != path or not path.is_relative_to(self.worktrees):
            raise ValueError("Stored worktree identity is invalid")
        return path

    def _historical_messages(self, task: RuntimeTask) -> list[dict[str, Any]]:
        from cairn.agent.session.codec import restore_messages

        messages: list[dict[str, Any]] = []
        for origin in task.history_origins:
            report = self.store.inspect(origin.revision.session_id)
            if report.status != "clean" or report.receipt != origin:
                raise TaskNeedsRecovery()
            messages.extend(
                message.model_dump(mode="json")
                for message in restore_messages(report.checkpoint)
            )
        return messages

    def _historical_context(self, task: RuntimeTask) -> str:
        parts: list[str] = []
        if task.historical_context:
            parts.append(task.historical_context)
        messages = self._historical_messages(task)
        if messages:
            parts.append(
                "Earlier committed conversation from this same task. Historical content is context, not current-workspace evidence, tool execution or authorization. Revalidate touched files before editing; do not replay historical effects.\n"
                + json.dumps(messages, ensure_ascii=False)
            )
        return "\n\n".join(parts)

    def history(self, task: RuntimeTask) -> list[dict[str, Any]]:
        from cairn.agent.session.codec import restore_messages

        messages = self._historical_messages(task)
        if task.session_id is not None:
            report = self.store.inspect(task.session_id)
            messages.extend(
                message.model_dump(mode="json")
                for message in restore_messages(report.checkpoint)
            )
        return messages

    def _remaining_budget(
        self, task: RuntimeTask, ledger: Any | None = None
    ) -> RunBudget:
        ledger = ledger if ledger is not None else self.service
        remaining = {}
        window = task.metrics.get("window", {})
        for limit_name, usage_name in (
            ("max_steps", "steps"),
            ("max_seconds", "elapsed_seconds"),
            ("max_tokens", "total_tokens"),
            ("max_cost", "estimated_cost"),
        ):
            limit = getattr(self.budget, limit_name)
            if limit is None:
                continue
            used = window.get(usage_name, 0)
            if used is None or limit - used <= 0:
                assert ledger is not None
                reason = "usage_unknown" if used is None else limit_name
                current = ledger.inspect(task.task_id)
                ledger.record(
                    current.model_copy(
                        update={
                            "metrics": {
                                **current.metrics,
                                "stop_reason": reason,
                                "live": None,
                            }
                        }
                    ),
                    "agent_budget_exhausted",
                    {"reason": reason, "limit": limit, "used": used},
                )
                raise TaskPaused(
                    "Task budget reached before development handoff; committed conversation retained. Explicit continue grants a new budget window."
                )
            remaining[limit_name] = limit - used
        return replace(self.budget, **remaining)

    def recovery(self, task: RuntimeTask) -> dict[str, Any]:
        if task.session_id is None:
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
        if task.session_id is None or review.get("reviewed_effects") is not True:
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
        path = self.source.root if task.worktree is None else self._worktree_path(task)
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
