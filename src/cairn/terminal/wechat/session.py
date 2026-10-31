"""In-session /wx controller: optional background login plus WeChat serving.

`/wx` is not a prerequisite CLI flow. It starts login in the background, renders
the whole QR in the terminal, and lets the operator use `/wx status`, `/wx
verify <code>` and `/wx stop` while waiting. A confirmed login automatically
starts the same single-task WeChat service in this Cairn process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from pathlib import Path
from typing import Any

from cairn.infra.wechat.ilink import ILinkClient
from cairn.infra.wechat.state import (
    WechatAccount,
    WechatState,
    ensure_outside_workspace,
)
from cairn.runtime.lifecycle import await_owned
from cairn.terminal.action import Action, correction, note
from cairn.terminal.wechat.login import run_login
from cairn.terminal.wechat.qr import render_ascii_qr
from cairn.terminal.wechat.serve import ServeConfig, TaskExecutor, serve_wechat


class WechatSessionController:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        task_executor: TaskExecutor,
        emit: Callable[[str], object],
        clear: Callable[[int], object] | None = None,
        show_panel: Callable[[Sequence[str]], object] | None = None,
        clear_panel: Callable[[], object] | None = None,
        prompt_verify: Callable[[str], str] | None = None,
        allow_insecure_local_http: bool = False,
        login_client: object | None = None,
        on_account_changed: Callable[[], None] | None = None,
        redact_text: Callable[[str], str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.state_dir = state_dir
        self.task_executor = task_executor
        self.emit = emit
        self.clear = clear
        self.show_panel = show_panel
        self.clear_panel = clear_panel
        self._qr_lines = 0
        self._status_history: list[str] = []
        self.allow_insecure_local_http = allow_insecure_local_http
        self.login_client = login_client
        self.on_account_changed = on_account_changed
        self.redact_text = redact_text
        self._state = WechatState(state_dir)
        ensure_outside_workspace(state_dir, workspace)
        self._task: asyncio.Task[object] | None = None
        self._stop: asyncio.Event | None = None
        self._login_task: asyncio.Task[WechatAccount] | None = None
        self._login_finish_task: asyncio.Task[None] | None = None
        self._verify_event = asyncio.Event()
        self._verify_code = ""
        self._verify_requested = False
        self._allowed_senders: set[str] = set()
        self._pending_tasks: set[asyncio.Future[Any]] = set()
        self._closing = False

    def _schedule(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _say(self, text: str) -> None:
        result = self.emit(text)
        if isawaitable(result):
            self._schedule(result)

    def _clear_qr(self) -> None:
        if self._qr_lines and self.clear is not None:
            result = self.clear(self._qr_lines)
            if isawaitable(result):
                self._schedule(result)
        self._qr_lines = 0

    def _show_panel(self, lines: Sequence[str]) -> None:
        if self.show_panel is not None:
            result = self.show_panel(lines)
            if isawaitable(result):
                self._schedule(result)

    def _clear_panel(self) -> None:
        if self.clear_panel is not None:
            result = self.clear_panel()
            if isawaitable(result):
                self._schedule(result)

    async def command(self, args: str) -> Action:
        parts = args.split()
        verb = parts[0].lower() if parts else ""
        if verb == "stop":
            return await self._stop_all()
        if verb == "status":
            return self._status()
        if verb == "verify" and len(parts) == 2:
            return self._verify(parts[1])
        if verb == "allow" and len(parts) == 2:
            self._allowed_senders.add(parts[1])
            return note(f"已信任微信发送者: {parts[1]}")
        if self._login_task is not None and not self._login_task.done():
            hint = (
                "; 需要验证码时请输入 /wx verify <code>"
                if self._verify_requested
                else ""
            )
            return note(f"微信登录正在等待扫码{hint}; /wx stop 可取消。")
        if self._task is not None and not self._task.done():
            return note("微信已在运行; 使用 /wx status 查看, /wx stop 停止。")
        try:
            account = self._state.load_account()
        except ValueError as exc:
            return correction(f"微信状态不可用: {exc}")
        if account is None or verb == "login":
            self._begin_login()
            return note(
                "微信登录已开始: 请扫描终端二维码; /wx status 查看, /wx stop 取消。"
            )
        return await self._start(account)

    def _begin_login(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            return
        self._verify_requested = False
        self._verify_code = ""
        task = asyncio.create_task(self._login())
        self._login_task = task
        task.add_done_callback(self._login_done)

    def _login_done(self, task: asyncio.Task[WechatAccount]) -> None:
        self._login_finish_task = asyncio.create_task(self._finish_login(task))

    async def _finish_login(self, task: asyncio.Task[WechatAccount]) -> None:
        if self._login_task is task:
            self._login_task = None
        if task.cancelled():
            self._say("微信登录已取消。")
            return
        try:
            account = task.result()
        except Exception as exc:
            self._say(f"微信登录失败: {type(exc).__name__}: {exc}")
            return
        if self._closing:
            return
        if self.on_account_changed is not None:
            self.on_account_changed()
        action = await self._start(account)
        self._say(getattr(action, "message", "微信已启动。"))

    async def _login(self) -> WechatAccount:
        self._say("微信未登录, 正在获取二维码...")

        def on_qr(qr) -> None:  # type: ignore[no-untyped-def]
            self._say("请使用微信扫描下面的二维码(完整静区已包含): ")
            try:
                self._say(render_ascii_qr(qr.image_content or qr.qrcode))
            except Exception as exc:
                self._say(f"二维码渲染失败: {exc}")
            if qr.image_content:
                self._say(f"若二维码无法显示, 可访问: {qr.image_content}")

        def on_status(status: str) -> None:
            self._status_history.append(status)

        async def prompt_verify(_message: str) -> str:
            self._verify_requested = True
            self._verify_event.clear()
            self._say("需要手机验证码: 请输入 /wx verify <code>(或 /wx stop 取消)。")
            await self._verify_event.wait()
            self._verify_requested = False
            return self._verify_code

        base_url = (
            getattr(self.login_client, "base_url", "https://ilinkai.weixin.qq.com")
            if self.login_client is not None
            else "https://ilinkai.weixin.qq.com"
        )
        return await run_login(
            state_dir=self.state_dir,
            workspace=self.workspace,
            base_url=base_url,
            client=self.login_client,  # type: ignore[arg-type]
            prompt_verify=prompt_verify,
            on_qr=on_qr,
            on_status=on_status,
            allow_insecure_local_base=self.allow_insecure_local_http,
        )

    def _verify(self, code: str) -> Action:
        if self._login_task is None or self._login_task.done():
            return note("当前没有等待验证码的微信登录。")
        if not self._verify_requested:
            return note("当前登录还没有要求验证码。")
        self._verify_code = code
        self._verify_event.set()
        return note("已提交验证码, 继续登录。")

    async def _start(self, account: WechatAccount) -> Action:
        senders = set(self._allowed_senders)
        if account.ilink_user_id:
            senders.add(account.ilink_user_id)
        if not senders:
            return correction(
                "登录响应没有用户 ID; 请使用 /wx allow <sender-id> 指定受信任发送者。"
            )
        self._stop = asyncio.Event()
        config = ServeConfig(
            workspace=self.workspace,
            state_dir=self.state_dir,
            account=account,
            approval=None,
            allowed_senders=frozenset(senders),
            poll_interval=0.2,
            redact_text=self.redact_text,
        )
        task = asyncio.create_task(
            serve_wechat(
                config=config,
                state=self._state,
                task_executor=self.task_executor,
                stop_event=self._stop,
                allow_insecure_local_http=self.allow_insecure_local_http,
            )
        )
        self._task = task
        await asyncio.sleep(0.05)
        if task.done():
            exc = task.exception()
            self._task = None
            if exc is not None:
                return correction(f"微信服务启动失败: {type(exc).__name__}: {exc}")
            return correction("微信服务意外停止。")
        return note(
            f"微信已启动, 受信任发送者: {', '.join(sorted(senders))}。使用 /wx stop 停止。"
        )

    def _status(self) -> Action:
        lines: list[str] = []
        try:
            account = self._state.load_account()
        except ValueError as exc:
            lines.append(f"微信状态不可用: {exc}")
            account = None
        if account is None:
            lines.append("微信登录: 未登录")
        else:
            lines.append(f"微信登录: 已登录 (bot={account.ilink_bot_id})")
        if self._status_history:
            lines.append("登录状态历史: " + " -> ".join(self._status_history[-8:]))
        if self._login_task is not None and not self._login_task.done():
            suffix = "; 等待 /wx verify <code>" if self._verify_requested else ""
            lines.append(f"微信登录: 等待扫码{suffix}")
        if self._task is None:
            lines.append("微信服务: 未运行")
        elif not self._task.done():
            lines.append("微信服务: 运行中")
        elif self._task.cancelled():
            lines.append("微信服务: 已停止")
        else:
            service_error = self._task.exception()
            if service_error is not None:
                lines.append(
                    f"微信服务: 已停止 (异常: {type(service_error).__name__}: "
                    f"{service_error})"
                )
            else:
                lines.append("微信服务: 已停止")
        message = "\n".join(lines)
        if self.show_panel is not None:
            self._show_panel(lines)
            return note("微信状态已显示在临时面板。")
        return note(message)

    async def _stop_all(self) -> Action:
        self._clear_qr()
        self._clear_panel()
        stopped_login = False
        login = self._login_task
        if login is not None and not login.done():
            login.cancel()
            await await_owned(login)
            stopped_login = True
        self._login_task = None
        self._verify_requested = False
        task = self._task
        stopped_service = False
        if task is not None and not task.done():
            # A long-poll may be in flight; set the cooperative stop event and
            # cancel the owned task so /wx stop returns after cleanup promptly.
            if self._stop is not None:
                self._stop.set()
            task.cancel()
            await await_owned(task)
            stopped_service = True
        self._task = None
        if stopped_service:
            return note("微信已停止。")
        if stopped_login:
            return note("微信登录已取消。")
        return note("微信当前没有运行。")

    def recipient_id(self) -> str | None:
        try:
            account = self._state.load_account()
        except ValueError:
            return None
        return account.ilink_user_id or None if account is not None else None

    def secret_values(self) -> tuple[str, ...]:
        try:
            account = self._state.load_account()
        except ValueError:
            return ()
        return (account.bot_token,) if account is not None and account.bot_token else ()

    def account_id(self) -> str | None:
        try:
            account = self._state.load_account()
        except ValueError:
            return None
        return account.ilink_bot_id or None if account is not None else None

    def _latest_context(self, sender: str) -> str | None:
        try:
            messages = self._state.load().get("messages", {})
        except ValueError:
            return None
        latest: tuple[float, str] | None = None
        for record in messages.values():
            if not isinstance(record, dict) or record.get("sender") != sender:
                continue
            context = record.get("context_token")
            updated = record.get("updated")
            if isinstance(context, str) and context:
                stamp = float(updated) if isinstance(updated, (int, float)) else 0.0
                if latest is None or stamp >= latest[0]:
                    latest = (stamp, context)
        return latest[1] if latest is not None else None

    async def deliver_for_account(
        self, sender: str, text: str, account_id: str
    ) -> bool:
        """Send only through the exact account that claimed the task."""
        try:
            account = self._state.load_account()
        except ValueError:
            return False
        if (
            account is None
            or account.ilink_bot_id != account_id
            or not sender
            or not text
            or sender != account.ilink_user_id
        ):
            return False
        context = self._latest_context(sender)
        if not context:
            return False
        client = ILinkClient(
            base_url=account.base_url,
            bot_token=account.bot_token,
            allow_insecure_local=self.allow_insecure_local_http,
        )
        try:
            await client.send_text(to_user_id=sender, text=text, context_token=context)
            return True
        except Exception:
            return False
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    async def deliver(self, sender: str, text: str) -> bool:
        """Best-effort push through the currently selected account only."""
        account_id = self.account_id()
        if account_id is None:
            return False
        return await self.deliver_for_account(sender, text, account_id)

    async def close(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            await self._stop_all()
