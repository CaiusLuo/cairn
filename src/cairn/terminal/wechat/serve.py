"""Long-polling WeChat server loop around the existing task execution chain.

One account, one Workspace, one task at a time. Inbound text from trusted senders
is admitted through the durable state store before the cursor advances, so a
crash cannot silently lose a received task; a task already started is never
replayed after a restart. All execution uses the existing Provider approval,
Workspace trust, tool capabilities and ``run_turn`` path.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from cairn.core.task_board import TaskSource
from cairn.infra.agent_memory.store import (
    AgentMemoryStore,
    default_agent_home,
)
from cairn.infra.agent_memory.store import (
    ensure_outside_workspace as ensure_agent_home_outside_workspace,
)
from cairn.infra.config import default_provider, load_project_providers
from cairn.infra.mcp.config import McpServerConfig
from cairn.infra.schedule.store import ScheduleStore, default_schedule_dir
from cairn.infra.schedule.store import (
    ensure_outside_workspace as ensure_schedule_outside_workspace,
)
from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import (
    HostResolution,
    ProviderReference,
    SessionBinding,
    SessionStoreError,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.task_board.store import TaskBoardStore, default_task_board_dir
from cairn.infra.tools.base import Tool
from cairn.infra.wechat.ilink import (
    ILinkClient,
    ILinkError,
    validate_base_url,
)
from cairn.infra.wechat.state import (
    WechatAccount,
    WechatServeLock,
    WechatState,
    ensure_outside_workspace,
)
from cairn.infra.workspace.lease import WorkspaceLease
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.runtime.agent_memory.tools import (
    MemoryCorrectTool,
    MemoryForgetTool,
    MemoryRememberTool,
    MemorySearchTool,
)
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.lifecycle import await_owned, settle_owned
from cairn.runtime.prompts import system_prompt_for
from cairn.runtime.scheduling.requests import (
    Clarification,
    NotSchedule,
    OnceRequest,
    parse_once_request,
)
from cairn.runtime.scheduling.runner import ScheduleRunner
from cairn.runtime.scheduling.tools import (
    ScheduleListTool,
    ScheduleManageTool,
)
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_board.tools import (
    TaskCreateTool,
    TaskDetailTool,
    TaskListTool,
    TaskTodoTool,
    TaskUpdateTool,
)
from cairn.terminal.durable import open_store
from cairn.terminal.headless import (
    DEFAULT_CHECK_TIMEOUT_S,
    DEFAULT_MAX_STEPS,
    HeadlessResult,
    HeadlessStatus,
    ProviderApproval,
    _run_via_config_async,
)
from cairn.terminal.wechat.control import WechatControlServer

TaskExecutor = Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]
ConversationExecutor = Callable[[str, str, str], Awaitable[str]]


class ConversationExecutionError(RuntimeError):
    """A structured, user-safe chat failure; never a generic canned reply."""

    def __init__(
        self, category: str, detail: str, *, trace_id: str | None = None
    ) -> None:
        self.category = category
        self.detail = detail
        self.trace_id = trace_id
        super().__init__(f"{category}: {detail}")


def conversation_text(result: HeadlessResult) -> str:
    """Return the model text or raise a structured failure."""
    if result.status is not HeadlessStatus.COMPLETED:
        raise ConversationExecutionError(
            result.status.value,
            result.error or "conversation did not complete",
            trace_id=result.trace_id,
        )
    text = (result.final_response or "").strip()
    if not text:
        raise ConversationExecutionError(
            "empty_response",
            "model produced no text reply",
            trace_id=result.trace_id,
        )
    return text


class ConversationDecision(StrEnum):
    CHAT = "CHAT"
    TASK = "TASK"
    UNKNOWN = "UNKNOWN"


ConversationClassifier = Callable[[str], Awaitable[ConversationDecision]]


def parse_conversation_decision(value: object) -> ConversationDecision:
    """Parse exactly one of the three labels; malformed output is UNKNOWN."""
    if not isinstance(value, str):
        return ConversationDecision.UNKNOWN
    text = value.strip().strip('"').upper()
    try:
        return ConversationDecision(text)
    except ValueError:
        return ConversationDecision.UNKNOWN


#: An immediate receipt must not block the actual task indefinitely.
ACK_TIMEOUT_SECONDS = 5.0
MAX_QUEUED_INBOUND = 100
MAX_INTENT_TASKS = 100
MAX_CHAT_TASKS = 32
PROJECT_LEASE_FILENAME = "project-write.lock"
CLASSIFIER_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class ServeConfig:
    workspace: Path
    state_dir: Path
    account: WechatAccount
    approval: ProviderApproval | None
    allowed_senders: frozenset[str]
    allow_shell: bool = False
    allow_mcp: bool = False
    allow_network: bool = False
    check_command: str | None = None
    check_timeout: float = DEFAULT_CHECK_TIMEOUT_S
    max_steps: int = DEFAULT_MAX_STEPS
    trace_dir: Path | None = None
    selected_skills: tuple[str, ...] = ()
    mcp_configs: Sequence[McpServerConfig] = ()
    poll_timeout_ms: int | None = None
    poll_interval: float = 0.0
    max_polls: int | None = None
    schedule_store: Path | None = None
    schedule_poll_interval: float = 1.0
    redact_text: Callable[[str], str] | None = None


@dataclass(slots=True)
class ServeReport:
    polls: int = 0
    messages_seen: int = 0
    tasks_started: int = 0
    replies_sent: int = 0
    errors: int = 0
    interrupted: int = 0
    scheduled_jobs: int = 0
    scheduled_delivered: int = 0


@dataclass(frozen=True, slots=True)
class _Inbound:
    key: str | None
    sender: str
    context_token: str
    text: str | None
    stable: bool = True


def _batch_key(cursor: str, index: int, message: object) -> str:
    digest = hashlib.sha1(
        json.dumps(message, sort_keys=True, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"batch:{cursor or 'first'}:{index}:{digest}"


def _inbound(message: object) -> _Inbound | None:
    if not isinstance(message, dict):
        return None
    if message.get("group_id"):
        return None  # group chats are out of scope for this version
    message_type = message.get("message_type")
    if isinstance(message_type, int) and message_type != 1:
        return None  # ignore bot/self messages
    sender = message.get("from_user_id")
    context = message.get("context_token")
    message_id = message.get("message_id")
    client_id = message.get("client_id")
    if isinstance(message_id, int) and message_id > 0:
        key = f"msg:{message_id}"
    elif isinstance(client_id, str) and client_id:
        key = f"client:{client_id}"
    else:
        key = None
    text = None
    items = message.get("item_list")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type") != 1:
                continue
            text_item = item.get("text_item")
            if isinstance(text_item, dict) and isinstance(text_item.get("text"), str):
                text = text_item["text"]
                break
    return _Inbound(
        key=key,
        sender=str(sender or ""),
        context_token=str(context or ""),
        text=text,
    )


def _format_result(result: HeadlessResult) -> str:
    lines = [f"任务 {result.task_id}", f"状态: {result.status.value}"]
    if result.verified:
        lines.append("宿主检查: 通过")
    if result.final_response:
        body = result.final_response.strip()
        if len(body) > 3000:
            body = body[:3000] + "..."
        lines.append("")
        lines.append(body)
    if result.error_category:
        lines.append(f"错误类别: {result.error_category}")
    if result.trace_id:
        lines.append(f"trace: {result.trace_id}")
    text = "\n".join(lines)
    return text[:4000]


def _known_credential_values(
    workspace: Path, extra: Sequence[str] = ()
) -> tuple[str, ...]:
    """Only declared provider credential values plus explicit extras.

    ``.env`` often contains ordinary settings (DEBUG=1); treating every value as
    a secret would redact unrelated text in memory and results.
    """
    values: list[str] = []
    env_file = dotenv_values(workspace / ".env")
    try:
        project = load_project_providers(workspace / ".cairn" / "models.toml")
        provider = default_provider(project, os.environ, env_file)
        names = {
            named.config.api_key_env for named in (project.providers or (provider,))
        }
    except (OSError, ValueError):
        names = set()
    for name in names:
        value = os.environ.get(name) or env_file.get(name)
        if isinstance(value, str) and value:
            values.append(value)
    for value in extra:
        if isinstance(value, str) and value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def default_task_executor(
    config: ServeConfig,
    *,
    project_lease: WorkspaceLease | None = None,
    on_secrets_resolved: Callable[[Sequence[str]], None] | None = None,
    task_source: TaskSource = TaskSource.WECHAT,
) -> TaskExecutor:
    if task_source is not TaskSource.WECHAT and task_source is not TaskSource.SCHEDULE:
        raise ValueError("unsupported task source for WeChat executor")
    agent_home = default_agent_home()
    ensure_agent_home_outside_workspace(agent_home, config.workspace)
    personal_memory = PersonalMemoryService(AgentMemoryStore(agent_home))
    known_secret_values: list[str] = [config.account.bot_token]
    personal_memory.add_secret_values(tuple(known_secret_values))
    memory_tools = (
        MemorySearchTool(personal_memory),
        MemoryRememberTool(personal_memory),
        MemoryForgetTool(personal_memory),
        MemoryCorrectTool(personal_memory),
    )
    task_board_root = default_task_board_dir()
    ensure_outside_workspace(task_board_root, config.workspace)
    task_board = TaskBoardService(
        TaskBoardStore(task_board_root),
        project=str(config.workspace),
        session_id=f"wechat:{config.workspace}",
        source=TaskSource.WECHAT,
    )

    def board_tools(service: TaskBoardService) -> tuple[Tool, ...]:
        return (
            TaskCreateTool(service),
            TaskDetailTool(service),
            TaskListTool(service),
            TaskUpdateTool(service),
            TaskTodoTool(service),
        )

    wechat_task_tools = board_tools(task_board)
    schedule_store = ScheduleStore(config.schedule_store or default_schedule_dir())
    schedule_tools = (
        ScheduleCreateTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
            redact=personal_memory.redact,
            require_trusted_message=True,
        ),
        ScheduleListTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
        ),
        ScheduleManageTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
        ),
    )

    def remember_secrets(values: Sequence[str]) -> None:
        for value in values:
            if isinstance(value, str) and value and value not in known_secret_values:
                known_secret_values.append(value)
        personal_memory.add_secret_values(values)
        if on_secrets_resolved is not None:
            on_secrets_resolved(values)

    session_root = config.state_dir / "sessions"
    chat_session_root = config.state_dir / "sessions-chat"
    ensure_outside_workspace(session_root, config.workspace)
    ensure_outside_workspace(chat_session_root, config.workspace)
    # Preserve legacy chat history instead of silently starting empty. A chat
    # store is seeded from the existing store once; later turns remain separate.
    chat_store_file = chat_session_root / "sessions.sqlite3"
    task_store_file = session_root / "sessions.sqlite3"
    if not chat_store_file.exists() and task_store_file.exists():
        chat_session_root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(task_store_file, chat_store_file)
        except OSError as exc:
            raise ValueError("cannot migrate existing WeChat chat history") from exc
    retained_sessions: list[DurableSession] = []
    retained_stores: list[SessionStore] = []

    def close_sessions(sessions: list[DurableSession]) -> None:
        while sessions:
            sessions[-1].close()
            sessions.pop()

    def release_sessions(sessions: list[DurableSession]) -> None:
        try:
            close_sessions(sessions)
        except BaseException:
            # Keep ownership and its writer lock for an idle retry.
            retained_sessions.extend(sessions)
            sessions.clear()
            raise

    def cleanup_retained() -> None:
        close_sessions(retained_sessions)
        while retained_stores:
            retained_stores[-1].close()
            retained_stores.pop()

    def sender_session_id(sender: str, *, purpose: str = "task") -> str:
        digest = hashlib.sha256(
            f"wechat:{config.workspace}:{sender}:{purpose}".encode()
        ).digest()
        return str(uuid.UUID(bytes=digest[:16], version=4))

    def legacy_session_id(sender: str) -> str:
        digest = hashlib.sha256(f"wechat:{config.workspace}:{sender}".encode()).digest()
        return str(uuid.UUID(bytes=digest[:16], version=4))

    def durable_factory(
        sender: str,
        active_sessions: list[DurableSession],
        *,
        purpose: str = "task",
    ) -> Callable[[Agent], DurableSession]:
        session_id = (
            legacy_session_id(sender)
            if purpose == "chat"
            else sender_session_id(sender, purpose=purpose)
        )
        purpose_steps = (
            config.max_steps if purpose == "task" else min(config.max_steps, 20)
        )

        def provider_reference(agent: Agent) -> ProviderReference | None:
            manager = agent.model_executor.manager
            if manager is None:
                return None
            group = manager.current_model()
            provider_config = manager.config
            return ProviderReference(
                provider=agent.provider_name or "",
                model_group=group.name,
                model_ids=group.model_ids,
                config_digest=hashlib.sha256(
                    json.dumps(
                        [
                            provider_config.base_url,
                            provider_config.api_key_env,
                            group.name,
                            group.model_ids,
                        ]
                    ).encode("utf-8")
                ).hexdigest(),
                system_prompt_digest=hashlib.sha256(
                    agent.system_prompt.encode("utf-8")
                ).hexdigest(),
                context_budget=agent.context_builder.budget.max_tokens,
                output_reserve=agent.context_builder.budget.response_tokens,
                max_steps=purpose_steps,
            )

        def secret_keys(agent: Agent) -> frozenset[str]:
            provider = agent.repo_context_provider
            if provider is None:
                raise ValueError("Durable WeChat session requires a workspace observer")
            return provider.secret_env_keys

        def make_binding(agent: Agent) -> SessionBinding:
            return SessionBinding(
                session_id=session_id,
                workspace=observe_workspace(
                    Workspace(config.workspace), secret_keys(agent)
                ),
                provider=provider_reference(agent),
                observed_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )

        def build(agent: Agent) -> DurableSession:
            # A check/repair may start another Turn before the overall request
            # returns. Release the settled Session before opening its successor.
            close_sessions(active_sessions)
            store_root = session_root if purpose == "task" else chat_session_root
            store = open_store(
                store_root,
                Workspace(config.workspace),
                secret_env_keys=secret_keys(agent),
                sensitive_values=tuple(known_secret_values),
            )
            session: DurableSession | None = None
            try:
                observation = make_binding(agent)
                try:
                    store.inspect(session_id)
                except SessionStoreError as exc:
                    if exc.code not in {"session_missing", "session_not_found"}:
                        raise
                    session = DurableSession.create(
                        store,
                        agent=agent,
                        binding=observation,
                        observe_binding=lambda: make_binding(agent),
                    )
                else:
                    session = DurableSession.reopen(
                        store,
                        session_id,
                        agent=agent,
                        binding=observation,
                        observe_binding=lambda: make_binding(agent),
                    )
                    report = session.inspect()
                    if report.status == "recovery_required":
                        unresolved = report.unresolved
                        if unresolved is None:
                            raise SessionStoreError("recovery_required")
                        final_binding = make_binding(agent)
                        session.reconcile(
                            report,
                            host_resolution=HostResolution(
                                session_id=session_id,
                                turn_id=unresolved.intent.turn_id,
                                actor="wechat-host",
                                resolved_at_utc=datetime.now(UTC)
                                .isoformat()
                                .replace("+00:00", "Z"),
                                binding=final_binding,
                                note="stale WeChat session recovered; turn abandoned without replay",
                            ),
                        )
                        report = session.inspect()
                    if report.status != "clean":
                        raise SessionStoreError("recovery_required")
                    final_binding = make_binding(agent)
                    session.reconcile(
                        report,
                        host_resolution=HostResolution(
                            session_id=session_id,
                            turn_id=None,
                            actor="wechat-host",
                            resolved_at_utc=datetime.now(UTC)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            binding=final_binding,
                        ),
                    )
                active_sessions.append(session)
                return session
            except BaseException as primary:
                try:
                    if session is None:
                        store.close()
                    else:
                        session.close()
                except BaseException as cleanup:
                    if session is None:
                        retained_stores.append(store)
                    else:
                        retained_sessions.append(session)
                    primary.add_note(
                        f"Durable session cleanup failed: {type(cleanup).__name__}."
                    )
                raise

        return build

    async def _execute(
        prompt: str, cancel: asyncio.Event, task_id: str, *, sender: str = ""
    ) -> HeadlessResult:
        cleanup_retained()
        task_tools = wechat_task_tools
        if task_source is TaskSource.SCHEDULE:
            task_tools = board_tools(
                TaskBoardService(
                    TaskBoardStore(task_board_root),
                    project=str(config.workspace),
                    session_id=f"schedule:{task_id}",
                    source=TaskSource.SCHEDULE,
                )
            )
        elif sender:
            task_tools = board_tools(
                TaskBoardService(
                    TaskBoardStore(task_board_root),
                    project=str(config.workspace),
                    session_id=f"wechat:{sender_session_id(sender)}",
                    source=TaskSource.WECHAT,
                )
            )
        active_sessions: list[DurableSession] = []
        try:
            result = await _run_via_config_async(
                workspace=config.workspace,
                prompt=prompt,
                task_id=task_id,
                grant_shell=config.allow_shell,
                approval=config.approval,
                max_steps=config.max_steps,
                trace_dir=config.trace_dir,
                check_command=config.check_command,
                check_timeout=config.check_timeout,
                selected_skills=config.selected_skills,
                mcp_configs=config.mcp_configs,
                grant_mcp=config.allow_mcp,
                grant_network=config.allow_network,
                sandbox_shell=config.allow_shell,
                cancellation_event=cancel,
                on_secrets_resolved=remember_secrets,
                personal_context_provider=personal_memory.render_context,
                extra_tools=(*memory_tools, *task_tools, *schedule_tools),
                durable_session_factory=durable_factory(sender, active_sessions)
                if sender
                else None,
            )
        except BaseException as primary:
            try:
                release_sessions(active_sessions)
            except BaseException as cleanup:
                primary.add_note(
                    f"Durable session cleanup failed: {type(cleanup).__name__}."
                )
            raise
        else:
            release_sessions(active_sessions)
            return result

    project_lease = project_lease or WorkspaceLease(
        config.state_dir / PROJECT_LEASE_FILENAME
    )

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str, *, sender: str = ""
    ) -> HeadlessResult:
        if not project_lease.acquire():
            cleanup_retained()
            return HeadlessResult(
                task_id=task_id,
                status=HeadlessStatus.RUNTIME_ERROR,
                workspace=str(config.workspace),
                error_category="project_busy",
                error="project_busy: another Cairn worker owns the workspace write lease.",
            )
        try:
            return await _execute(prompt, cancel, task_id, sender=sender)
        finally:
            project_lease.release()

    execute.durable_factory = durable_factory  # type: ignore[attr-defined]
    execute.project_lease = project_lease  # type: ignore[attr-defined]
    return execute


def default_conversation_classifier(config: ServeConfig) -> ConversationClassifier:
    """Bounded semantic classifier; malformed output is UNKNOWN, never TASK."""

    async def classify(text: str) -> ConversationDecision:
        bounded = text[:2000]
        prompt = (
            "Classify the user message. Return strict JSON only: "
            '{"intent":"CHAT|TASK|UNKNOWN"}. CHAT means progress, status or '
            "ordinary conversation. TASK means new work, including an action "
            "phrased as a question. UNKNOWN means genuinely ambiguous.\n"
            f"MESSAGE: {bounded}"
        )
        try:
            result = await asyncio.wait_for(
                _run_via_config_async(
                    workspace=config.workspace,
                    prompt=prompt,
                    task_id="wechat-intent",
                    system_prompt=system_prompt_for("classification"),
                    grant_shell=False,
                    approval=config.approval,
                    max_steps=1,
                    trace_dir=config.trace_dir,
                    read_only=True,
                    extra_tools=(),
                    durable_session_factory=None,
                    provider_name=None,
                ),
                timeout=CLASSIFIER_TIMEOUT_SECONDS,
            )
        except Exception:
            raise
        if result.error is not None or result.status.value != "completed":
            raise RuntimeError(
                result.error or f"classifier status {result.status.value}"
            )
        try:
            payload = json.loads((result.final_response or "").strip())
        except (TypeError, ValueError):
            return ConversationDecision.UNKNOWN
        if not isinstance(payload, dict) or set(payload) != {"intent"}:
            return ConversationDecision.UNKNOWN
        return parse_conversation_decision(payload["intent"])

    return classify


def close_conversation_sessions(sessions: list[DurableSession]) -> BaseException | None:
    """Close every conversation-owned session; return the first cleanup error."""
    first_error: BaseException | None = None
    while sessions:
        session = sessions.pop()
        try:
            session.close()
        except BaseException as exc:
            first_error = first_error or exc
    return first_error


def default_conversation_executor(
    config: ServeConfig,
    durable_factory: object | None = None,
    project_lease: WorkspaceLease | None = None,
) -> ConversationExecutor:
    """Full-capability background conversation; same provider/tool policy as tasks."""
    agent_home = default_agent_home()
    ensure_agent_home_outside_workspace(agent_home, config.workspace)
    personal_memory = PersonalMemoryService(AgentMemoryStore(agent_home))
    memory_tools: tuple[Tool, ...] = (
        MemorySearchTool(personal_memory),
        MemoryRememberTool(personal_memory),
        MemoryForgetTool(personal_memory),
        MemoryCorrectTool(personal_memory),
    )
    task_board = TaskBoardService(
        TaskBoardStore(default_task_board_dir()),
        project=str(config.workspace),
        session_id=f"wechat:{config.workspace}",
        source=TaskSource.WECHAT,
    )
    task_tools: tuple[Tool, ...] = (
        TaskCreateTool(task_board),
        TaskDetailTool(task_board),
        TaskListTool(task_board),
        TaskUpdateTool(task_board),
        TaskTodoTool(task_board),
    )
    schedule_store = ScheduleStore(config.schedule_store or default_schedule_dir())
    schedule_tools = (
        ScheduleCreateTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
            redact=personal_memory.redact,
            require_trusted_message=True,
        ),
        ScheduleListTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
        ),
        ScheduleManageTool(
            store=schedule_store,
            workspace=str(config.workspace),
            recipient=lambda: config.account.ilink_user_id or None,
            account_id=lambda: config.account.ilink_bot_id,
        ),
    )

    project_lease = project_lease or WorkspaceLease(
        config.state_dir / PROJECT_LEASE_FILENAME
    )

    async def execute(question: str, sender: str, snapshot: str) -> str:
        active_sessions: list[DurableSession] = []
        factory = (
            durable_factory(sender, active_sessions, purpose="chat")
            if callable(durable_factory)
            else None
        )
        cleanup_error: BaseException | None = None
        try:
            result = await _run_via_config_async(
                workspace=config.workspace,
                prompt=(
                    "You are the persistent Cairn assistant for this WeChat sender. "
                    "You have the same tools and memory as task work. Use tools when "
                    "needed. The bounded current-task facts are:\n"
                    f"{snapshot}\nUser message:\n{question}"
                ),
                task_id=f"wechat-chat-{sender[-8:]}",
                grant_shell=config.allow_shell,
                approval=config.approval,
                max_steps=min(config.max_steps, 20),
                trace_dir=config.trace_dir,
                cancellation_event=None,
                personal_context=personal_memory.render_context(),
                read_only=False,
                sandbox_shell=config.allow_shell,
                system_prompt=system_prompt_for("chat"),
                conversation_only=factory is not None,
                tool_lease=project_lease,
                durable_session_factory=factory,
                extra_tools=(*memory_tools, *task_tools, *schedule_tools),
            )
        finally:
            cleanup_error = close_conversation_sessions(active_sessions)
        if cleanup_error is not None:
            raise ConversationExecutionError(
                "session_cleanup_failed",
                type(cleanup_error).__name__,
            ) from cleanup_error
        return conversation_text(result)

    return execute


class _Service:
    def __init__(
        self,
        *,
        config: ServeConfig,
        state: WechatState,
        client: ILinkClient,
        executor: TaskExecutor,
        report: ServeReport,
        stop_event: asyncio.Event | None,
        redact_text: Callable[[str], str] | None = None,
        conversation_executor: ConversationExecutor | None = None,
        conversation_classifier: ConversationClassifier | None = None,
        progress_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.executor = executor
        self.report = report
        self.stop_event = stop_event
        self.redact_text = redact_text
        self._executor_takes_sender = "sender" in inspect.signature(executor).parameters
        self._current: asyncio.Task[None] | None = None
        self._current_key: str | None = None
        self._current_started = 0.0
        self._current_cancel: asyncio.Event | None = None
        self._stopping = False
        self._queue_error: Exception | None = None
        self._conversation_executor = conversation_executor
        self._conversation_classifier = conversation_classifier
        self._progress_provider = progress_provider
        self._schedule_store = schedule_store or (
            ScheduleStore(config.schedule_store) if config.schedule_store else None
        )
        self._chat_tasks: set[asyncio.Task[None]] = set()
        self._intent_tasks: set[asyncio.Task[None]] = set()
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._intent_locks: dict[str, asyncio.Lock] = {}
        self._clarifications: dict[str, str] = {}
        self._typing_tickets: dict[str, str] = {}

    async def _reply(
        self, sender: str, context: str, text: str, *, retries: int = 2
    ) -> bool:
        if not sender or not context:
            return False
        if self.redact_text is not None:
            text = self.redact_text(text)
        for attempt in range(retries):
            try:
                await self.client.send_text(
                    to_user_id=sender, text=text, context_token=context
                )
                self.report.replies_sent += 1
                return True
            except ILinkError:
                if attempt == 0:
                    await asyncio.sleep(0.5)
        self.report.errors += 1
        return False

    async def _safe_reply(
        self, sender: str, context: str, text: str, *, retries: int = 2
    ) -> bool:
        with contextlib.suppress(Exception):
            return await self._reply(sender, context, text, retries=retries)
        return False

    async def _queued_count(self) -> int:
        return await asyncio.to_thread(
            lambda: len(self.state.records_in_states(frozenset({"queueing", "queued"})))
        )

    async def _status_text(self) -> str:
        queued = await self._queued_count()
        if self._current is None:
            return f"当前没有运行中的任务\uff0c排队 {queued} 项。"
        elapsed = max(0.0, time.monotonic() - self._current_started)
        return f"任务正在执行(已运行 {elapsed:.0f}s)\uff0c排队 {queued} 项。"

    async def _cancel_current(self) -> bool:
        task = self._current
        if task is None:
            return False
        if self._current_cancel is not None:
            self._current_cancel.set()
        task.cancel()
        await await_owned(task)
        return True

    async def _send_typing(self, inbound: _Inbound, status: int) -> None:
        try:
            ticket = self._typing_tickets.get(inbound.sender)
            if ticket is None:
                config = await self.client.get_config(
                    ilink_user_id=inbound.sender,
                    context_token=inbound.context_token,
                )
                ticket_value = config.get("typing_ticket")
                if not isinstance(ticket_value, str) or not ticket_value:
                    return
                ticket = ticket_value
                self._typing_tickets[inbound.sender] = ticket
            await self.client.send_typing(
                ilink_user_id=inbound.sender,
                typing_ticket=ticket,
                status=status,
            )
        except Exception:
            return

    @staticmethod
    def _is_conversation(text: str) -> bool:
        """Classify progress/chat without stealing clear execution requests."""
        lowered = text.casefold()
        execution_verbs = (
            "实现",
            "修复",
            "处理",
            "添加",
            "创建",
            "开发",
            "执行",
            "运行",
            "修改",
            "更新",
            "提交",
            "部署",
            "帮我做",
            "做一个",
            "写一个",
        )
        if any(verb in lowered for verb in execution_verbs):
            return False
        markers = (
            "进展",
            "进度",
            "怎么样",
            "如何",
            "为什么",
            "什么",
            "还要多久",
            "完成了吗",
            "卡住",
            "阻塞",
            "状态",
            "你好",
            "在吗",
            "谢谢",
            "辛苦",
            "blocked",
            "progress",
            "status",
        )
        return (
            any(marker in lowered for marker in markers)
            or "?" in text
            or "\uff1f" in text
        )

    def _progress_snapshot(self) -> str:
        record = self.state.get_record(self._current_key or "") or {}
        queued = len(self.state.records_in_states(frozenset({"queueing", "queued"})))
        elapsed = (
            time.monotonic() - self._current_started if self._current is not None else 0
        )
        raw_text = str(record.get("text", "")).strip().replace("\n", " ")[:160]
        task_text = self.redact_text(raw_text) if self.redact_text else raw_text
        status = str(record.get("state", "idle"))
        progress = self._progress_provider() if self._progress_provider else ""
        if progress:
            progress = self.redact_text(progress) if self.redact_text else progress
        return (
            f"当前任务状态={status}; 已运行={elapsed:.0f}s; 排队={queued}; "
            f"任务摘要={task_text or '-'}; 最近事件={progress or '-'}"
        )

    async def _classify_conversation(self, text: str) -> ConversationDecision:
        if self._conversation_classifier is None:
            # Internal callers that did not opt into semantic chat keep the
            # conservative work-dispatch behavior.
            return ConversationDecision.TASK
        return await self._conversation_classifier(text)

    async def _conversation_call(self, inbound: _Inbound, snapshot: str) -> None:
        assert inbound.text is not None
        lock = self._chat_locks.setdefault(inbound.sender, asyncio.Lock())
        async with lock:
            self.state.mark(
                inbound.key or "", "chat_pending", command=inbound.text[:40]
            )
            try:
                if self._conversation_executor is None:
                    answer = f"当前进度\uff1a{snapshot}"
                else:
                    answer = await self._conversation_executor(
                        inbound.text, inbound.sender, snapshot
                    )
            except asyncio.CancelledError:
                # Service shutdown must not turn an already-accepted chat into
                # a user cancellation; a send attempt has its own unknown state.
                record = self.state.get_record(inbound.key or "") or {}
                if record.get("state") == "chat_sending":
                    self.state.mark(inbound.key or "", "chat_delivery_unknown")
                else:
                    self.state.mark(inbound.key or "", "chat_pending")
                raise
            except ConversationExecutionError as exc:
                detail = exc.detail
                if self.redact_text is not None:
                    detail = self.redact_text(detail)
                self.state.mark(
                    inbound.key or "",
                    "chat_failed",
                    error_type="conversation",
                    error_category=exc.category,
                    error=detail[:160],
                    trace_id=exc.trace_id,
                )
                if not self._stopping:
                    await self._safe_reply(
                        inbound.sender,
                        inbound.context_token,
                        f"本轮没有生成有效回复\uff08{exc.category}\uff09\uff1a{detail[:160]}",
                    )
                return
            except SessionStoreError as exc:
                detail = str(exc)
                if self.redact_text is not None:
                    detail = self.redact_text(detail)
                self.state.mark(
                    inbound.key or "",
                    "chat_failed",
                    error_type=type(exc).__name__,
                    error_category="session_store",
                    error=detail[:160],
                )
                if not self._stopping:
                    await self._safe_reply(
                        inbound.sender,
                        inbound.context_token,
                        f"本轮聊天会话不可用\uff08{exc.code}\uff09\uff0c任务未受影响。",
                    )
                return
            except Exception as exc:
                detail = str(exc)
                if self.redact_text is not None:
                    detail = self.redact_text(detail)
                self.state.mark(
                    inbound.key or "",
                    "chat_failed",
                    error_type=type(exc).__name__,
                    error_category="runtime_error",
                    error=detail[:160],
                )
                if not self._stopping:
                    await self._safe_reply(
                        inbound.sender,
                        inbound.context_token,
                        f"本轮聊天失败\uff08{type(exc).__name__}\uff09\uff1a{detail[:160]}",
                    )
                return
            if not self._stopping and answer.strip():
                # Persist the send attempt before I/O. A crash or lost
                # acknowledgement must not cause a blind replay of the reply.
                self.state.mark(inbound.key or "", "chat_sending")
                try:
                    sent = await self._safe_reply(
                        inbound.sender, inbound.context_token, answer
                    )
                except asyncio.CancelledError:
                    self.state.mark(inbound.key or "", "chat_delivery_unknown")
                    raise
                self.state.mark(
                    inbound.key or "",
                    "chat_replied" if sent else "chat_delivery_unknown",
                )
            else:
                self.state.mark(inbound.key or "", "chat_empty")

    def _start_conversation(self, inbound: _Inbound) -> asyncio.Task[None]:
        if len(self._chat_tasks) >= MAX_CHAT_TASKS:
            self.state.mark(inbound.key or "", "rejected", reason="chat_backlog_full")

            async def _reply_busy() -> None:
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    "当前聊天请求过多\uff0c请稍后再试。",
                )

            task = asyncio.create_task(_reply_busy())
            self._intent_tasks.add(task)
            task.add_done_callback(self._intent_tasks.discard)
            return task
        task = asyncio.create_task(
            self._conversation_call(inbound, self._progress_snapshot())
        )
        self._chat_tasks.add(task)

        def _remove(done: asyncio.Task[None]) -> None:
            self._chat_tasks.discard(done)
            with contextlib.suppress(Exception):
                done.exception()

        task.add_done_callback(_remove)
        return task

    def _start_intent(self, inbound: _Inbound) -> None:
        if len(self._intent_tasks) >= MAX_INTENT_TASKS:
            self.state.mark(inbound.key or "", "rejected", reason="intent_backlog_full")

            async def _reply_busy() -> None:
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    "当前消息处理排队过多\uff0c请稍后再试。",
                )

            task = asyncio.create_task(_reply_busy())
            self._intent_tasks.add(task)
            task.add_done_callback(self._intent_tasks.discard)
            return
        task = asyncio.create_task(self._run_intent(inbound))
        self._intent_tasks.add(task)

        def _remove(done: asyncio.Task[None]) -> None:
            self._intent_tasks.discard(done)
            with contextlib.suppress(Exception):
                done.exception()

        task.add_done_callback(_remove)

    async def _run_intent(self, inbound: _Inbound) -> None:
        lock = self._intent_locks.setdefault(inbound.sender, asyncio.Lock())
        try:
            async with lock:
                # The lock protects classifier ordering only. Queue/chat work
                # performs acknowledgement I/O outside it so one slow send
                # cannot block the next message's classification.
                decision = await self._classify_conversation(inbound.text or "")
            if decision is ConversationDecision.CHAT:
                self._start_conversation(inbound)
            elif decision is ConversationDecision.TASK:
                await self._queue_inbound(inbound)
            else:
                self._ask_clarification(inbound)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Classification failure is per-message and must not stop the
            # WeChat service or execute the message as work.
            self.state.mark(
                inbound.key or "",
                "classification_failed",
                error_type=type(exc).__name__,
            )
            if not self._stopping:
                detail = str(exc)
                if self.redact_text is not None:
                    detail = self.redact_text(detail)
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    f"暂时无法判断意图\uff08{type(exc).__name__}: {detail[:160]}\uff09。"
                    "请重试或明确说明是查询当前任务还是安排新任务。",
                )

    def _ask_clarification(self, inbound: _Inbound) -> None:
        assert inbound.key is not None
        self._clarifications[inbound.sender] = inbound.key
        self.state.mark(inbound.key, "clarifying", command=(inbound.text or "")[:40])
        self._schedule_clarification(inbound)

    def _schedule_clarification(self, inbound: _Inbound) -> None:
        async def send() -> None:
            if not self._stopping:
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    "这条消息的意图不明确\uff1a你是想查询当前任务进度\uff0c还是安排一个新任务\uff1f",
                )

        task = asyncio.create_task(send())
        self._intent_tasks.add(task)
        task.add_done_callback(self._intent_tasks.discard)

    async def _resolve_clarification(self, inbound: _Inbound) -> bool:
        key = self._clarifications.get(inbound.sender)
        if key is None:
            return False
        record = self.state.get_record(key) or {}
        original = self._validate_recovery_record(key, record) if record else None
        if original is None:
            self._clarifications.pop(inbound.sender, None)
            return False
        text = " ".join((inbound.text or "").strip().casefold().split())
        current = replace(original, context_token=inbound.context_token)
        if text in {"查询当前任务", "查询进度", "查看进度", "查看状态", "查询"}:
            if not self.state.resolve_clarification(
                key,
                inbound.key or "",
                target_state="chat_pending",
                context_token=inbound.context_token,
            ):
                return False
            self._clarifications.pop(inbound.sender, None)
            await self._start_conversation(current)
            return True
        if text in {"新任务", "安排新任务", "执行新任务", "排队", "排队执行"}:
            if not self.state.resolve_clarification(
                key,
                inbound.key or "",
                target_state="queued",
                context_token=inbound.context_token,
            ):
                return False
            self._clarifications.pop(inbound.sender, None)
            await self._queue_inbound(current)
            return True
        # A different message is not swallowed: classify it on its own.
        return False

    async def _run_task(self, key: str, inbound: _Inbound) -> None:
        assert inbound.text is not None
        cancel = asyncio.Event()
        self._current_cancel = cancel
        self._current_started = time.monotonic()
        task_id = "wechat-" + (key.split(":", 1)[1] if ":" in key else key)
        try:
            record = self.state.get_record(key) or {}
            if not record.get("ack_attempted"):
                # The task is admitted and owns the single execution slot now.
                # Mark the attempt before I/O so a crash cannot ack twice, and
                # keep the wait bounded so a slow receipt cannot block execution.
                self.state.mark(key, "started", ack_attempted=True)
                # Do not echo inbound text before the full trusted redactor and
                # provider approval are available. A generic receipt cannot leak
                # a key prefix at a truncation boundary; the task's content is
                # explained later in the normal result message.
                try:
                    acknowledged = await asyncio.wait_for(
                        self._safe_reply(
                            inbound.sender,
                            inbound.context_token,
                            "已收到\uff0c正在安排处理",
                            retries=1,
                        ),
                        timeout=ACK_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    acknowledged = False
                if acknowledged:
                    self.state.mark(key, "started", ack_sent=True, ack_state="sent")
                else:
                    # One attempt only: iLink send_text has no stable client_id
                    # identity here, so a retry could duplicate the receipt when
                    # the first response was lost. Record the uncertainty; do
                    # not lose or replay the task.
                    self.state.mark(key, "started", ack_state="unknown")
            await self._send_typing(inbound, 1)
            if self._executor_takes_sender:
                result = await self.executor(  # type: ignore[call-arg]
                    inbound.text, cancel, task_id, sender=inbound.sender
                )
            else:
                result = await self.executor(inbound.text, cancel, task_id)
        except asyncio.CancelledError:
            self.state.mark(key, "cancelled")
            if not self._stopping:
                await self._safe_reply(
                    inbound.sender, inbound.context_token, "任务已取消(清理完成)。"
                )
            raise
        except Exception as exc:
            self.state.mark(key, "failed", error_type=type(exc).__name__)
            if not self._stopping:
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    f"任务执行失败({type(exc).__name__}), 未声称成功。",
                )
            return
        else:
            self.state.mark(
                key,
                result.status.value,
                task_id=result.task_id,
                trace_id=result.trace_id,
                verified=result.verified,
            )
            if not self._stopping:
                await self._safe_reply(
                    inbound.sender, inbound.context_token, _format_result(result)
                )
        finally:
            await self._send_typing(inbound, 2)

    def _start_task(self, key: str, inbound: _Inbound) -> None:
        self.state.mark(
            key, "started", sender=inbound.sender, context_token=inbound.context_token
        )
        self.report.tasks_started += 1
        task = asyncio.create_task(self._run_task(key, inbound))
        self._current = task
        self._current_key = key

        def _clear(done: asyncio.Task[None]) -> None:
            # A task can be cancelled before its coroutine gets its first step.
            try:
                if done.cancelled():
                    record = self.state.get_record(key) or {}
                    if record.get("state") == "started":
                        self.state.mark(key, "cancelled")
                else:
                    error = done.exception()
                    if error is not None:
                        raise error
                if self._current is task:
                    self._current = None
                    self._current_key = None
                    self._current_cancel = None
                    self._start_next_queued()
            except Exception as exc:
                # Persistence/dispatch failures stop admission instead of losing
                # a queued task or silently launching later work.
                self._queue_error = exc
                self._stopping = True

        task.add_done_callback(_clear)

    def _start_next_queued(self) -> None:
        if (
            self._stopping
            or self._current is not None
            or (self.stop_event is not None and self.stop_event.is_set())
        ):
            return
        for key, record in self.state.records_in_state("queued"):
            sender = record.get("sender")
            context = record.get("context_token")
            text = record.get("text")
            if (
                not isinstance(sender, str)
                or not isinstance(context, str)
                or not context
                or not isinstance(text, str)
                or record.get("is_text", bool(text)) is not True
                or record.get("stable", True) is not True
            ):
                raise ValueError("WeChat queued message record is damaged")
            if sender not in self.config.allowed_senders:
                self.state.mark(key, "rejected", reason="untrusted_sender")
                continue
            self._start_task(
                key, _Inbound(key=key, sender=sender, context_token=context, text=text)
            )
            return

    async def _queue_inbound(self, inbound: _Inbound) -> None:
        assert inbound.key is not None
        key = inbound.key
        queued = await self._queued_count()
        if queued >= MAX_QUEUED_INBOUND:
            self.state.mark(key, "rejected", reason="queue_full")
            await self._safe_reply(
                inbound.sender,
                inbound.context_token,
                "当前排队任务过多\uff0c请稍后再试。",
            )
            return
        record = self.state.get_record(key) or {}
        if record.get("state") == "queued":
            self._start_next_queued()
            return
        if record.get("state") == "queueing" or record.get("ack_attempted"):
            # A lost acknowledgement is not repeated, but the unstarted task
            # remains eligible for execution.
            self.state.mark(key, "queued", ack_state=record.get("ack_state", "unknown"))
            self._start_next_queued()
            return

        self.state.mark(key, "queueing", ack_attempted=True)
        text = (
            "已收到\uff0c已加入队列"
            if self._current is not None
            else "已收到\uff0c正在安排处理"
        )
        try:
            acknowledged = await asyncio.wait_for(
                self._safe_reply(
                    inbound.sender, inbound.context_token, text, retries=1
                ),
                timeout=ACK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            acknowledged = False
        self.state.mark(
            key,
            "queued",
            ack_sent=acknowledged,
            ack_state="sent" if acknowledged else "unknown",
        )
        self._start_next_queued()

    def _start_schedule_task(
        self, coro: Coroutine[Any, Any, None], inbound: _Inbound
    ) -> None:
        if len(self._intent_tasks) >= MAX_INTENT_TASKS:
            self.state.mark(
                inbound.key or "", "rejected", reason="schedule_backlog_full"
            )
            return
        task = asyncio.create_task(coro)
        self._intent_tasks.add(task)
        task.add_done_callback(self._intent_tasks.discard)

    async def _schedule_notice(self, inbound: _Inbound, text: str) -> None:
        await self._safe_reply(inbound.sender, inbound.context_token, text)

    async def _run_host_schedule(self, inbound: _Inbound, parsed: OnceRequest) -> None:
        record = self.state.get_record(inbound.key or "") or {}
        received = record.get("received_at")
        if not isinstance(received, (int, float)):
            self.state.mark(
                inbound.key or "",
                "schedule_clarification",
                reason="missing_received_at",
            )
            await self._safe_reply(
                inbound.sender,
                inbound.context_token,
                "这条定时请求缺少可信的首次接收时间\uff0c无法安全创建\uff1b请重新发送。",
            )
            return
        if record.get("schedule_receipt_state") in {"sending", "sent", "unknown"}:
            return
        owner = self.config.account.ilink_user_id
        if not owner or inbound.sender != owner:
            self.state.mark(
                inbound.key or "", "schedule_rejected", reason="not_account_owner"
            )
            await self._safe_reply(
                inbound.sender,
                inbound.context_token,
                "当前微信账号不是该调度任务的 owner\uff0c不能创建会永久无人执行的任务。",
            )
            return
        payload = self.redact_text(parsed.prompt) if self.redact_text else parsed.prompt
        digest = hashlib.sha256(
            f"{self.config.workspace}:{inbound.sender}:{self.config.account.ilink_bot_id}:{inbound.key}".encode()
        ).hexdigest()[:12]
        job = ScheduleJob(
            job_id=f"job-{digest}",
            name=payload[:60],
            prompt=payload,
            workspace=str(self.config.workspace),
            recipient=inbound.sender,
            account_id=self.config.account.ilink_bot_id,
            kind=ScheduleKind.ONCE,
            at=parsed.at,
            timezone="Asia/Shanghai",
            reminder=parsed.reminder,
            created_at=datetime.fromtimestamp(float(received), tz=UTC),
            next_run_at=parsed.at,
        )
        assert self._schedule_store is not None
        saved, _created = await asyncio.to_thread(
            self._schedule_store.add_or_get_job, job
        )
        self.state.mark(
            inbound.key or "",
            "schedule_created",
            schedule_job_id=saved.job_id,
            schedule_receipt_state="sending",
        )
        sent = await self._safe_reply(
            inbound.sender,
            inbound.context_token,
            f"已创建定时任务 {saved.job_id}\uff1a"
            f"{saved.next_run_at.isoformat() if saved.next_run_at else '-'}"
            f"\uff1b内容\uff1a{saved.prompt[:200]}\uff1b"
            f"类型\uff1a{'提醒' if saved.reminder else '执行'}\u3002"
            f"取消请使用 /schedule remove {saved.job_id}。",
        )
        self.state.mark(
            inbound.key or "",
            "schedule_receipt_sent" if sent else "schedule_receipt_unknown",
            schedule_job_id=saved.job_id,
            schedule_receipt_state="sent" if sent else "unknown",
        )

    def _try_host_once_schedule(self, inbound: _Inbound) -> bool:
        """Start host-owned one-shot handling without blocking receive."""
        if self._schedule_store is None or inbound.key is None:
            return False
        record = self.state.get_record(inbound.key) or {}
        if record.get("state") in {
            "schedule_pending",
            "schedule_created",
            "schedule_receipt_sent",
            "schedule_receipt_unknown",
        } or record.get("schedule_receipt_state") in {"sending", "sent", "unknown"}:
            return True
        parsed = parse_once_request(
            inbound.text or "",
            float(record.get("received_at", 0)),
            message_id=inbound.key,
        )
        if isinstance(parsed, NotSchedule):
            return False
        if isinstance(parsed, Clarification):
            self.state.mark(inbound.key, "schedule_clarification", reason=parsed.reason)
            self._start_schedule_task(
                self._schedule_notice(
                    inbound,
                    f"无法创建定时任务\uff1a{parsed.reason}\u3002请明确时间和要做的事。",
                ),
                inbound,
            )
            return True
        self.state.mark(inbound.key, "schedule_pending")
        self._start_schedule_task(self._run_host_schedule(inbound, parsed), inbound)
        return True

    async def _handle(self, inbound: _Inbound) -> None:
        if not inbound.sender or inbound.context_token == "":
            if inbound.key is not None:
                self.state.mark(inbound.key, "rejected", reason="missing_context")
            if inbound.sender:
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    "缺少会话上下文, 未启动任务。",
                )
            return
        if inbound.sender not in self.config.allowed_senders:
            if inbound.key is not None:
                self.state.mark(inbound.key, "rejected", reason="untrusted_sender")
            return
        if inbound.key is None or not inbound.stable:
            if inbound.key is not None:
                self.state.mark(inbound.key, "rejected", reason="no_stable_id")
            await self._safe_reply(
                inbound.sender, inbound.context_token, "消息缺少稳定 ID, 未启动任务。"
            )
            return
        record = self.state.get_record(inbound.key)
        if record is not None and record.get("state") == "clarification_consumed":
            return
        if inbound.text is None:
            self.state.mark(inbound.key, "rejected", reason="non_text")
            await self._safe_reply(
                inbound.sender, inbound.context_token, "仅支持文本消息, 未启动任务。"
            )
            return
        command = inbound.text.strip().lower()
        if command == "/status":
            self.state.mark(inbound.key, "command", command="/status")
            await self._safe_reply(
                inbound.sender, inbound.context_token, await self._status_text()
            )
            return
        if command == "/cancel":
            self.state.mark(inbound.key, "command", command="/cancel")
            if await self._cancel_current():
                await self._safe_reply(
                    inbound.sender,
                    inbound.context_token,
                    "已取消当前任务(清理完成)。",
                )
            else:
                await self._safe_reply(
                    inbound.sender, inbound.context_token, "当前没有可取消的任务。"
                )
            return
        if await self._resolve_clarification(inbound):
            return
        if self._try_host_once_schedule(inbound):
            return
        if self._conversation_classifier is None:
            decision = await self._classify_conversation(inbound.text)
            if decision is ConversationDecision.CHAT:
                self._start_conversation(inbound)
            elif decision is ConversationDecision.TASK:
                await self._queue_inbound(inbound)
            else:
                self._ask_clarification(inbound)
            return
        self.state.mark(inbound.key, "classifying", command=command[:40])
        self._start_intent(inbound)

    def _validate_recovery_record(
        self, key: str, record: dict[str, object]
    ) -> _Inbound:
        sender = record.get("sender")
        context = record.get("context_token")
        text = record.get("text")
        # ``get(..., default)`` preserves the legacy default only when the key
        # is absent. An explicit null is a present value and must be rejected.
        is_text = record.get("is_text", bool(text))
        stable = record.get("stable", True)
        if not isinstance(is_text, bool) or not isinstance(stable, bool):
            raise ValueError("WeChat message record is damaged")
        if (
            not isinstance(key, str)
            or not isinstance(sender, str)
            or not sender
            or not isinstance(context, str)
            or not context
            or (is_text and not isinstance(text, str))
        ):
            raise ValueError("WeChat message record is damaged")
        return _Inbound(
            key=key,
            sender=sender,
            context_token=context,
            text=text if is_text and isinstance(text, str) else None,
            stable=stable,
        )

    async def _recover(self) -> None:
        for key, record in self.state.records_in_state("started"):
            self.state.mark(key, "interrupted")
            self.report.interrupted += 1
            sender = str(record.get("sender", ""))
            context = str(record.get("context_token", ""))
            if sender not in self.config.allowed_senders:
                continue
            await self._safe_reply(
                sender,
                context,
                "上次任务在服务重启/中断时状态不确定, 已标记中断, 不会自动重放。",
            )
        pending = self.state.records_in_states(
            frozenset(
                {
                    "received",
                    "queueing",
                    "queued",
                    "classifying",
                    "chat_pending",
                    "chat_sending",
                    "chat_interrupted",
                    "clarifying",
                }
            )
        )
        # Validate the whole recovery set before any task is started or any
        # acknowledgement is sent. A damaged record is a startup error, not a
        # record to skip silently.
        recovered_items = [
            (key, record, self._validate_recovery_record(key, record))
            for key, record in pending
        ]
        for _key, record, recovered in recovered_items:
            state = record.get("state")
            if state == "queued":
                continue
            if state == "clarifying":
                sender = recovered.sender
                self._clarifications[sender] = _key
                continue
            if state == "chat_sending":
                self.state.mark(_key, "chat_delivery_unknown")
                continue
            if state in {"chat_pending", "chat_interrupted"}:
                self._start_conversation(recovered)
                continue
            if state == "classifying":
                self._start_intent(recovered)
                continue
            await self._handle(recovered)
        self._start_next_queued()

    async def _wait_or_stop(self, delay: float) -> bool:
        if self.stop_event is None:
            await asyncio.sleep(delay)
            return False
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def _poll_updates(self) -> dict[str, Any] | None:
        poll = asyncio.create_task(
            self.client.get_updates(
                self.state.get_cursor(),
                longpoll_timeout_ms=self.config.poll_timeout_ms,
            )
        )
        if self.stop_event is None:
            return await poll
        stop = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {poll, stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop in done and not poll.done():
                await settle_owned(poll)
                return None
            if poll.done():
                return poll.result()
            return await poll
        finally:
            # Every exit path owns both children: an external cancellation at
            # ``asyncio.wait`` must not leave a live long-poll behind. Cancelling
            # stop and poll is bounded and repeated cancellation is absorbed by
            # settle_owned; the original CancelledError still propagates.
            await settle_owned(poll, stop)

    async def run(self) -> ServeReport:
        ensure_outside_workspace(self.config.state_dir, self.config.workspace)
        with contextlib.suppress(ILinkError):
            await self.client.notify_start()
        try:
            await self._recover()
        except BaseException:
            self._stopping = True
            with contextlib.suppress(Exception):
                await self._cancel_current()
            raise
        consecutive_errors = 0
        try:
            while not self._stopping:
                if self.stop_event is not None and self.stop_event.is_set():
                    break
                if (
                    self.config.max_polls is not None
                    and self.report.polls >= self.config.max_polls
                ):
                    break
                self.report.polls += 1
                try:
                    data = await self._poll_updates()
                    if data is None:
                        break
                    consecutive_errors = 0
                except ILinkError:
                    self.report.errors += 1
                    consecutive_errors += 1
                    if await self._wait_or_stop(min(5.0, 0.2 * consecutive_errors)):
                        break
                    continue
                ret = data.get("ret")
                errcode = data.get("errcode")
                if ret not in (None, 0) or errcode not in (None, 0):
                    self.report.errors += 1
                    await asyncio.sleep(1.0)
                    continue
                batch = data.get("msgs")
                messages = batch if isinstance(batch, list) else []
                self.report.messages_seen += len(messages)
                cursor_before = self.state.get_cursor()
                next_cursor = data.get("get_updates_buf")
                pending: list[tuple[str, _Inbound, bool]] = []
                # Persist every message's processing fact BEFORE advancing the
                # batch-wide get_updates_buf cursor, so a crash cannot silently
                # drop a received message.
                for index, message in enumerate(messages):
                    parsed = _inbound(message)
                    if parsed is None:
                        self.state.mark(
                            _batch_key(cursor_before, index, message),
                            "ignored",
                            reason="unparseable_or_group_or_self",
                        )
                        continue
                    stable = parsed.key is not None
                    key = parsed.key or _batch_key(cursor_before, index, message)
                    stored = _Inbound(
                        key=key,
                        sender=parsed.sender,
                        context_token=parsed.context_token,
                        text=parsed.text,
                        stable=stable,
                    )
                    self.state.record_received(
                        key,
                        sender=stored.sender,
                        context_token=stored.context_token,
                        text=stored.text or "",
                        stable=stable,
                        is_text=stored.text is not None,
                    )
                    pending.append((key, stored, stable))
                if isinstance(next_cursor, str) and next_cursor:
                    self.state.set_cursor(next_cursor)
                if self.stop_event is not None and self.stop_event.is_set():
                    # Persist the batch/cursor but do not start new work.
                    break
                for key, inbound, _stable in pending:
                    stored_record = self.state.get_record(key)
                    if stored_record is None:
                        continue
                    if stored_record.get("state") != "received":
                        continue
                    await self._handle(inbound)
                if (
                    not messages
                    and self.config.poll_interval
                    and await self._wait_or_stop(self.config.poll_interval)
                ):
                    break
        finally:
            self._stopping = True
            await self._cancel_current()
            owned = (*tuple(self._intent_tasks), *tuple(self._chat_tasks))
            if owned:
                await settle_owned(*owned)
            with contextlib.suppress(ILinkError, asyncio.CancelledError):
                notify = asyncio.create_task(self.client.notify_stop())
                await await_owned(notify)
        if self._queue_error is not None:
            raise self._queue_error
        return self.report


async def serve_wechat(
    *,
    config: ServeConfig,
    client: ILinkClient | None = None,
    state: WechatState | None = None,
    task_executor: TaskExecutor | None = None,
    conversation_executor: ConversationExecutor | None = None,
    conversation_classifier: ConversationClassifier | None = None,
    progress_provider: Callable[[], str] | None = None,
    stop_event: asyncio.Event | None = None,
    allow_insecure_local_http: bool = False,
) -> ServeReport:
    # Validate the host state location and account endpoint before reading any
    # state, creating a lock or opening a network client.
    ensure_outside_workspace(config.state_dir, config.workspace)
    validate_base_url(
        config.account.base_url, allow_insecure_local=allow_insecure_local_http
    )
    active_client = (
        client
        if client is not None
        else ILinkClient(
            base_url=config.account.base_url,
            bot_token=config.account.bot_token,
            allow_insecure_local=allow_insecure_local_http,
        )
    )
    active_state = state if state is not None else WechatState(config.state_dir)
    known_secrets: list[str] = [config.account.bot_token]

    def remember_known_secrets(values: Sequence[str]) -> None:
        for value in values:
            if isinstance(value, str) and value and value not in known_secrets:
                known_secrets.append(value)

    def redact_text(text: str) -> str:
        cleaned = config.redact_text(text) if config.redact_text is not None else text
        for value in known_secrets:
            if value:
                cleaned = cleaned.replace(value, "[REDACTED]")
        return cleaned

    project_lease = WorkspaceLease(config.state_dir / PROJECT_LEASE_FILENAME)
    executor = (
        task_executor
        if task_executor is not None
        else default_task_executor(
            config,
            project_lease=project_lease,
            on_secrets_resolved=remember_known_secrets,
        )
    )
    if conversation_executor is None and task_executor is None:
        conversation_executor = default_conversation_executor(
            config,
            getattr(executor, "durable_factory", None),
            project_lease=project_lease,
        )
    if conversation_classifier is None and task_executor is None:
        conversation_classifier = default_conversation_classifier(config)
    report = ServeReport()
    task_gate = asyncio.Lock()

    executor_takes_sender = "sender" in inspect.signature(executor).parameters

    async def gated_executor(
        prompt: str, cancel: asyncio.Event, task_id: str, *, sender: str = ""
    ) -> HeadlessResult:
        async with task_gate:
            if executor_takes_sender:
                return await executor(  # type: ignore[call-arg]
                    prompt, cancel, task_id, sender=sender
                )
            return await executor(prompt, cancel, task_id)

    scheduler_task: asyncio.Task[object] | None = None
    scheduler_stop: asyncio.Event | None = None
    scheduler_runner: ScheduleRunner | None = None
    if config.schedule_store is not None:
        ensure_schedule_outside_workspace(config.schedule_store, config.workspace)

        async def deliver_scheduled(sender: str, text: str) -> bool:
            if sender not in config.allowed_senders:
                return False
            context = active_state.latest_context(sender)
            if not context:
                return False
            try:
                await active_client.send_text(
                    to_user_id=sender, text=text, context_token=context
                )
                return True
            except Exception:
                return False

        # The account's login identity is not an authorization: only a sender
        # explicitly present in this serve invocation may receive work.
        scheduler_owner = (
            config.account.ilink_user_id
            if config.account.ilink_user_id in config.allowed_senders
            else ""
        )
        scheduled_executor = (
            task_executor
            if task_executor is not None
            else default_task_executor(
                config,
                project_lease=project_lease,
                on_secrets_resolved=remember_known_secrets,
                task_source=TaskSource.SCHEDULE,
            )
        )

        async def gated_schedule_executor(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            async with task_gate:
                job_id = task_id.removeprefix("scheduled-")
                if scheduler_runner is not None:
                    scheduler_runner.store.mark_execution_started(job_id)
                return await scheduled_executor(prompt, cancel, task_id)

        scheduler_runner = ScheduleRunner(
            store=ScheduleStore(config.schedule_store),
            executor=gated_schedule_executor,
            deliver=deliver_scheduled,
            recipient=lambda: scheduler_owner or None,
            poll_interval=config.schedule_poll_interval,
            workspace=str(config.workspace.resolve()),
            account_id=config.account.ilink_bot_id,
            result_redactor=redact_text,
        )
        scheduler_stop = None

    def control_snapshot(method: str) -> dict[str, object]:
        messages = active_state.load().get("messages", {})
        records = [
            {"key": key, **record}
            for key, record in messages.items()
            if isinstance(record, dict)
        ]
        if method == "tasks":
            return {
                "tasks": [
                    record
                    for record in records
                    if str(record.get("state", "")).startswith("task")
                    or record.get("state")
                    in {"queued", "queueing", "started", "interrupted"}
                ]
            }
        if method == "chat":
            return {
                "chat": [
                    record
                    for record in records
                    if str(record.get("state", "")).startswith("chat")
                    or record.get("state") in {"clarifying", "classification_failed"}
                ]
            }
        current = next(
            (
                record
                for record in records
                if record.get("state") in {"started", "queueing"}
            ),
            None,
        )
        session_id = os.environ.get("CAIRN_WECHAT_SESSION_ID", "")
        if not session_id:
            with contextlib.suppress(OSError, ValueError):
                owner = json.loads(
                    (config.state_dir / "background-owner.json").read_text(
                        encoding="utf-8"
                    )
                )
                if isinstance(owner, dict):
                    session_id = str(owner.get("session_id", ""))
        return {
            "pid": os.getpid(),
            "session_id": session_id,
            "polls": report.polls,
            "messages_seen": report.messages_seen,
            "tasks_started": report.tasks_started,
            "queued": sum(
                1 for record in records if record.get("state") in {"queued", "queueing"}
            ),
            "current": current,
        }

    service = _Service(
        config=config,
        state=active_state,
        client=active_client,
        executor=gated_executor,
        report=report,
        stop_event=stop_event,
        redact_text=redact_text,
        conversation_executor=conversation_executor,
        conversation_classifier=conversation_classifier,
        progress_provider=progress_provider,
    )
    control = WechatControlServer(
        config.state_dir / "control.sock",
        control_snapshot,
    )
    stored_account = active_state.load_account()
    if (
        stored_account is not None
        and stored_account.ilink_bot_id != config.account.ilink_bot_id
    ):
        raise ValueError("WeChat state belongs to a different bot account")
    lock = WechatServeLock(config.state_dir)
    lock.acquire(
        description=(
            f"pid={os.getpid()} bot={config.account.ilink_bot_id} "
            f"workspace={config.workspace}"
        )
    )
    # Start background work only after every identity/directory/lock check
    # succeeded. A failed startup must leave no executor task behind.
    if scheduler_runner is not None:
        scheduler_stop = asyncio.Event()
        scheduler_task = asyncio.create_task(scheduler_runner.run(scheduler_stop))
    try:
        with contextlib.suppress(OSError):
            await control.start()
        return await service.run()
    finally:
        await control.close()
        if scheduler_task is not None:
            if scheduler_stop is not None:
                scheduler_stop.set()
            scheduler_task.cancel()
            await await_owned(scheduler_task)
            if scheduler_runner is not None:
                report.scheduled_jobs = scheduler_runner.report.jobs_run
                report.scheduled_delivered = scheduler_runner.report.delivered
        try:
            close_task = asyncio.create_task(active_client.close())
            await await_owned(close_task)
        finally:
            lock.release()
