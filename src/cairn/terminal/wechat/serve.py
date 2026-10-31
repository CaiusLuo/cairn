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
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from cairn.infra.agent_memory.store import (
    AgentMemoryStore,
    default_agent_home,
)
from cairn.infra.agent_memory.store import (
    ensure_outside_workspace as ensure_agent_home_outside_workspace,
)
from cairn.infra.config import default_provider, load_project_providers
from cairn.infra.mcp.config import McpServerConfig
from cairn.infra.schedule.store import ScheduleStore
from cairn.infra.schedule.store import (
    ensure_outside_workspace as ensure_schedule_outside_workspace,
)
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
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.runtime.lifecycle import await_owned
from cairn.runtime.scheduling.runner import ScheduleRunner
from cairn.terminal.headless import (
    DEFAULT_CHECK_TIMEOUT_S,
    DEFAULT_MAX_STEPS,
    HeadlessResult,
    ProviderApproval,
    _run_via_config_async,
)

TaskExecutor = Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]


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
    on_secrets_resolved: Callable[[Sequence[str]], None] | None = None,
) -> TaskExecutor:
    agent_home = default_agent_home()
    ensure_agent_home_outside_workspace(agent_home, config.workspace)
    personal_memory = PersonalMemoryService(AgentMemoryStore(agent_home))
    personal_memory.add_secret_values((config.account.bot_token,))

    def remember_secrets(values: Sequence[str]) -> None:
        personal_memory.add_secret_values(values)
        if on_secrets_resolved is not None:
            on_secrets_resolved(values)

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
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
            cancellation_event=cancel,
            on_secrets_resolved=remember_secrets,
            personal_context_provider=personal_memory.render_context,
        )
        with contextlib.suppress(Exception):
            personal_memory.record_task(
                prompt=prompt,
                result=result,
                task_id=task_id,
                trace_id=result.trace_id,
            )
        return result

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
    ) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.executor = executor
        self.report = report
        self.stop_event = stop_event
        self.redact_text = redact_text
        self._current: asyncio.Task[None] | None = None
        self._current_key: str | None = None
        self._current_started = 0.0
        self._current_cancel: asyncio.Event | None = None
        self._stopping = False
        self._startup_pending: list[tuple[str, dict[str, Any]]] = []
        self._typing_tickets: dict[str, str] = {}

    async def _reply(self, sender: str, context: str, text: str) -> None:
        if not sender or not context:
            return
        if self.redact_text is not None:
            text = self.redact_text(text)
        for attempt in range(2):
            try:
                await self.client.send_text(
                    to_user_id=sender, text=text, context_token=context
                )
                self.report.replies_sent += 1
                return
            except ILinkError:
                if attempt == 0:
                    await asyncio.sleep(0.5)
        self.report.errors += 1

    async def _safe_reply(self, sender: str, context: str, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._reply(sender, context, text)

    def _status_text(self) -> str:
        if self._current is None:
            return "当前没有运行中的任务。"
        elapsed = max(0.0, time.monotonic() - self._current_started)
        return f"任务正在执行(已运行 {elapsed:.0f}s)。"

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

    async def _run_task(self, key: str, inbound: _Inbound) -> None:
        assert inbound.text is not None
        cancel = asyncio.Event()
        self._current_cancel = cancel
        self._current_started = time.monotonic()
        task_id = "wechat-" + (key.split(":", 1)[1] if ":" in key else key)
        self.state.mark(
            key, "started", sender=inbound.sender, context_token=inbound.context_token
        )
        await self._send_typing(inbound, 1)
        try:
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

        def _clear(_: asyncio.Task[None]) -> None:
            if self._current is task:
                self._current = None
                self._current_key = None
                self._current_cancel = None

        task.add_done_callback(_clear)

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
                inbound.sender, inbound.context_token, self._status_text()
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
        if self._current is not None:
            self.state.mark(inbound.key, "busy")
            await self._safe_reply(
                inbound.sender,
                inbound.context_token,
                "当前有任务正在执行, 请稍后重新发送。",
            )
            return
        self._start_task(inbound.key, inbound)

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
        self._startup_pending = self.state.records_in_state("received")

    async def run(self) -> ServeReport:
        ensure_outside_workspace(self.config.state_dir, self.config.workspace)
        with contextlib.suppress(ILinkError):
            await self.client.notify_start()
        await self._recover()
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
                if self._current is None and self._startup_pending:
                    key, record = self._startup_pending.pop(0)
                    recovered = _Inbound(
                        key=key,
                        sender=str(record.get("sender", "")),
                        context_token=str(record.get("context_token", "")),
                        text=str(record.get("text", "")),
                        stable=bool(record.get("stable", True)),
                    )
                    await self._handle(recovered)
                    continue
                self.report.polls += 1
                try:
                    data = await self.client.get_updates(
                        self.state.get_cursor(),
                        longpoll_timeout_ms=self.config.poll_timeout_ms,
                    )
                    consecutive_errors = 0
                except ILinkError:
                    self.report.errors += 1
                    consecutive_errors += 1
                    await asyncio.sleep(min(5.0, 0.2 * consecutive_errors))
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
                    )
                    pending.append((key, stored, stable))
                if isinstance(next_cursor, str) and next_cursor:
                    self.state.set_cursor(next_cursor)
                for key, inbound, _stable in pending:
                    stored_record = self.state.get_record(key)
                    if stored_record is None:
                        continue
                    if stored_record.get("state") != "received":
                        continue
                    await self._handle(inbound)
                if not messages and self.config.poll_interval:
                    await asyncio.sleep(self.config.poll_interval)
        finally:
            self._stopping = True
            await self._cancel_current()
            with contextlib.suppress(ILinkError, asyncio.CancelledError):
                notify = asyncio.create_task(self.client.notify_stop())
                await await_owned(notify)
        return self.report


async def serve_wechat(
    *,
    config: ServeConfig,
    client: ILinkClient | None = None,
    state: WechatState | None = None,
    task_executor: TaskExecutor | None = None,
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

    executor = (
        task_executor
        if task_executor is not None
        else default_task_executor(config, on_secrets_resolved=remember_known_secrets)
    )
    report = ServeReport()
    task_gate = asyncio.Lock()

    async def gated_executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        async with task_gate:
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
        scheduler_runner = ScheduleRunner(
            store=ScheduleStore(config.schedule_store),
            executor=gated_executor,
            deliver=deliver_scheduled,
            recipient=lambda: scheduler_owner or None,
            poll_interval=config.schedule_poll_interval,
            workspace=str(config.workspace.resolve()),
            account_id=config.account.ilink_bot_id,
            result_redactor=redact_text,
        )
        scheduler_stop = None
    service = _Service(
        config=config,
        state=active_state,
        client=active_client,
        executor=gated_executor,
        report=report,
        stop_event=stop_event,
        redact_text=redact_text,
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
        return await service.run()
    finally:
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
