"""In-session /wx controller: optional background login plus WeChat serving.

`/wx` is not a prerequisite CLI flow. It starts login in the background, renders
the whole QR in the terminal, and lets the operator use `/wx status`, `/wx
verify <code>` and `/wx stop` while waiting. A confirmed login automatically
starts the same single-task WeChat service in this Cairn process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from pathlib import Path
from typing import Any

from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.wechat.ilink import ILinkClient, QRCode
from cairn.infra.wechat.state import (
    WechatAccount,
    WechatState,
    ensure_outside_workspace,
)
from cairn.runtime.lifecycle import await_owned
from cairn.shared.action import Action, correction, note
from cairn.terminal.wechat.login import run_login
from cairn.terminal.wechat.qr import render_ascii_qr
from cairn.terminal.wechat.serve import (
    ConversationClassifier,
    ConversationExecutor,
    ServeConfig,
    TaskExecutor,
    serve_wechat,
)


class WechatSessionController:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        task_executor: TaskExecutor,
        background_launcher: Callable[
            [WechatAccount, frozenset[str]], Awaitable[tuple[int, str]]
        ]
        | None = None,
        conversation_executor: ConversationExecutor | None = None,
        conversation_classifier: ConversationClassifier | None = None,
        progress_provider: object | None = None,
        emit: Callable[[str], object],
        clear: Callable[[int], object] | None = None,
        show_panel: Callable[[Sequence[str]], object] | None = None,
        clear_panel: Callable[[], object] | None = None,
        prompt_verify: Callable[[str], str] | None = None,
        allow_insecure_local_http: bool = False,
        login_client: object | None = None,
        on_account_changed: Callable[[], None] | None = None,
        redact_text: Callable[[str], str] | None = None,
        runtime_client: RuntimeClient | None = None,
        qr_handler: Callable[[QRCode | None], None] | None = None,
        discover_background: bool = True,
    ) -> None:
        self.runtime_client = runtime_client
        self.qr_handler = qr_handler
        self.workspace = workspace
        self.state_dir = state_dir
        self.task_executor = task_executor
        self.background_launcher = background_launcher
        self._background_path = state_dir / "background-owner.json"
        self._background_pid: int | None = None
        self._background_session_id: str | None = None
        self.conversation_executor = conversation_executor
        self.conversation_classifier = conversation_classifier
        self.progress_provider = progress_provider
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
        try:
            raw = (
                json.loads(self._background_path.read_text(encoding="utf-8"))
                if discover_background
                else {}
            )
            if isinstance(raw, dict) and isinstance(raw.get("pid"), int):
                self._background_pid = raw["pid"]
                self._background_session_id = str(raw.get("session_id", ""))
        except (OSError, ValueError):
            pass
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

    @property
    def state(self) -> WechatState:
        return self._state

    def _schedule(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _say(self, text: str) -> None:
        result = self.emit(text)
        if isawaitable(result):
            self._schedule(result)

    def _say_qr(self, text: str) -> None:
        """Emit QR-related lines and remember how many need clearing."""
        self._qr_lines += len(text.splitlines() or [""])
        self._say(text)

    def _clear_qr(self) -> None:
        if self.qr_handler is not None:
            self.qr_handler(None)
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
        if verb in {"help", "--help", "-h"}:
            return note(
                "/wx 登录并启动微信服务 (已有账号默认复用)\n"
                "/wx status 查看登录/服务状态\n"
                "/wx verify <code> 提交手机验证码\n"
                "/wx allow <sender> 追加可信发送者\n"
                "/wx stop 仅停止服务, 保留账号供下次复用\n"
                "/wx disconnect 停止服务并清理本地账号/游标, 下次重新扫码\n"
                "别名: /wx logout"
            )
        if verb in {"disconnect", "logout", "reset"}:
            return await self._disconnect()
        if verb == "stop":
            return await self._stop_all()
        if verb == "status":
            return self._status()
        if verb == "verify" and len(parts) == 2:
            return self._verify(parts[1])
        if verb == "allow" and len(parts) == 2:
            self._allowed_senders.add(parts[1])
            if (
                self.runtime_client is not None
                and self._task is not None
                and not self._task.done()
            ):
                account = self._state.load_account()
                await self._stop_all()
                if account is not None:
                    await self._start(account)
            return note(f"已信任微信发送者: {parts[1]}")
        if verb not in {"", "login"}:
            return correction(f"未知 /wx 子命令: {verb}; 使用 /wx help 查看可用命令。")
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
                "微信登录已开始: 请在 CLI 或 Web 查看二维码; /wx status 查看, /wx stop 取消。"
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
            self._clear_qr()
            self._say("微信登录已取消。")
            return
        try:
            account = task.result()
        except Exception as exc:
            self._clear_qr()
            self._say(f"微信登录失败: {type(exc).__name__}: {exc}")
            return
        if self._closing:
            return
        if self.on_account_changed is not None:
            self.on_account_changed()
        action = await self._start(account)
        # The QR belongs only to the pre-login phase. Clear it before reporting
        # success so the transcript does not keep a stale login image.
        self._clear_qr()
        message = getattr(action, "message", "微信已启动。")
        if getattr(action, "tone", None) is None or "已启动" in message:
            self._say(f"\x1b[32m微信登录成功: {message}\x1b[0m")
        else:
            self._say(message)

    async def _login(self) -> WechatAccount:
        self._say("微信未登录, 正在获取二维码...")

        def on_qr(qr) -> None:  # type: ignore[no-untyped-def]
            if self.qr_handler is not None:
                self.qr_handler(qr)
                return
            # A refreshed QR replaces the previous one instead of stacking images.
            self._clear_qr()
            self._say_qr("请使用微信扫描下面的二维码(完整静区已包含): ")
            try:
                self._say_qr(render_ascii_qr(qr.image_content or qr.qrcode))
            except Exception as exc:
                self._say_qr(f"二维码渲染失败: {exc}")
            if qr.image_content:
                self._say_qr(f"若二维码无法显示, 可访问: {qr.image_content}")

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

    def _background_alive(self) -> bool:
        if self._background_pid is None:
            return False
        try:
            os.kill(self._background_pid, 0)
            return True
        except OSError:
            self._background_pid = None
            self._background_session_id = None
            with contextlib.suppress(OSError):
                self._background_path.unlink()
            return False

    def _save_background(self, pid: int, session_id: str) -> None:
        self._background_pid = pid
        self._background_session_id = session_id
        self._background_log_path = self.state_dir / "background.log"
        self._background_path.parent.mkdir(parents=True, exist_ok=True)
        self._background_path.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "session_id": session_id,
                    "log": str(self._background_log_path),
                    "started_at": time.time(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            self._background_path.chmod(0o600)

    def _background_log_tail(self) -> str:
        path = self.state_dir / "background.log"
        try:
            data = path.read_bytes()
        except OSError:
            return "没有生成后台日志"
        text = data.decode("utf-8", errors="replace").strip()
        return text[-1000:] if text else "没有生成后台日志"

    async def _stop_background(self) -> bool:
        pid = self._background_pid
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self._background_pid = None
            return False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.05)
        self._background_pid = None
        self._background_session_id = None
        with contextlib.suppress(OSError):
            self._background_path.unlink()
        return True

    async def _start(self, account: WechatAccount) -> Action:
        senders = set(self._allowed_senders)
        if account.ilink_user_id:
            senders.add(account.ilink_user_id)
        if not senders:
            return correction(
                "登录响应没有用户 ID; 请使用 /wx allow <sender-id> 指定受信任发送者。"
            )
        if self._background_alive():
            return note(
                f"微信后台服务已在运行 (session={self._background_session_id}); "
                "使用 /wx status 查看\uff0c/wx stop 停止。"
            )
        if self.background_launcher is not None:
            with contextlib.suppress(OSError):
                (self.state_dir / "control.sock").unlink()
            pid, session_id = await self.background_launcher(
                account, frozenset(senders)
            )
            self._background_pid = pid
            self._background_session_id = session_id
            ready = False
            for _ in range(30):
                if not self._background_alive():
                    break
                if (self.state_dir / "control.sock").exists():
                    ready = True
                    break
                await asyncio.sleep(0.1)
            if not ready:
                detail = self._background_log_tail()
                await self._stop_background()
                with contextlib.suppress(OSError):
                    self._background_path.unlink()
                return correction(f"微信后台启动未就绪: {detail}")
            self._save_background(pid, session_id)
            return note(
                f"微信后台服务已启动 (pid={pid}, session={session_id}); "
                "退出当前界面不会停止它\uff0c/wx stop 才会停止。"
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
            runtime_url=self.runtime_client.url if self.runtime_client else None,
            runtime_client=self.runtime_client,
        )
        task = asyncio.create_task(
            serve_wechat(
                config=config,
                state=self._state,
                task_executor=self.task_executor,
                conversation_executor=self.conversation_executor,
                conversation_classifier=self.conversation_classifier,
                progress_provider=self.progress_provider,  # type: ignore[arg-type]
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

    async def _disconnect(self) -> Action:
        """Stop owned work and remove local account/cursor state."""
        await self._stop_all()
        try:
            self._state.clear_connection()
        except OSError as exc:
            return correction(f"微信本地状态清理失败: {exc}")
        self._allowed_senders.clear()
        self._status_history.append("disconnected")
        self._clear_panel()
        return note("微信已断开并清理本地账号/游标; 使用 /wx 重新扫码连接。")

    def _control_status(self) -> dict[str, object] | None:
        path = self.state_dir / "control.sock"
        if not path.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                sock.connect(str(path))
                sock.sendall(b'{"method":"status"}\n')
                raw = sock.recv(65536)
            payload = json.loads(raw.decode("utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
            return result if isinstance(result, dict) else None
        except (OSError, ValueError):
            return None

    def restore_senders(self, senders: tuple[str, ...]) -> None:
        self._allowed_senders.update(senders)

    def management_status(self) -> dict[str, object]:
        return {
            "account_saved": bool(self.account_id()),
            "running": self._task is not None and not self._task.done(),
            "login_pending": self._login_task is not None
            and not self._login_task.done(),
            "verification_required": self._verify_requested,
            "status": self._status().message,
        }

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
        if self._background_alive():
            lines.append(
                f"微信服务: 后台运行中 (pid={self._background_pid}, "
                f"session={self._background_session_id})"
            )
            lines.append(f"后台日志: {self.state_dir / 'background.log'}")
            control = self._control_status()
            if control is not None:
                lines.append(
                    "控制面: "
                    f"pid={control.get('pid')} session={control.get('session_id')} "
                    f"queued={control.get('queued')} polls={control.get('polls')}"
                )
        elif self._task is None:
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
        stopped_background = await self._stop_background()
        task = self._task
        stopped_service = stopped_background
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
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
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
        ):
            return False
        # Receiver admission and delivery must use the same trusted-sender
        # set.  The account owner is always allowed; /wx allow extends it.
        if sender != account.ilink_user_id and sender not in self._allowed_senders:
            return False
        context = context_token or self._latest_context(sender)
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
        # A detached background owner is explicitly not owned by this CLI
        # lifecycle. Closing the UI detaches; only /wx stop terminates it.
        if self._background_alive():
            return
        with contextlib.suppress(Exception):
            await self._stop_all()
