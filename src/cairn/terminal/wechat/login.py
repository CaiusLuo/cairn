"""Interactive iLink QR login for one WeChat bot account."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from pathlib import Path

from cairn.infra.wechat.ilink import (
    DEFAULT_BASE_URL,
    ILinkClient,
    ILinkError,
    QRCode,
    redirect_base_url,
    validate_base_url,
)
from cairn.infra.wechat.state import (
    WechatAccount,
    WechatState,
    ensure_outside_workspace,
)

Sleep = Callable[[float], Awaitable[None]]


class QRLoginError(RuntimeError):
    pass


async def run_login(
    *,
    state_dir: Path,
    workspace: Path | None = None,
    base_url: str = DEFAULT_BASE_URL,
    client: ILinkClient | None = None,
    prompt_verify: Callable[[str], str | Awaitable[str]] | None = None,
    on_qr: Callable[[QRCode], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    sleep: Sleep = asyncio.sleep,
    interval: float = 1.0,
    max_polls: int = 180,
    allow_insecure_local_base: bool = False,
) -> WechatAccount:
    """Drive the QR handshake and persist the confirmed account (0600)."""
    if workspace is not None:
        # Refuse a state dir inside the Workspace before any token is saved.
        ensure_outside_workspace(state_dir, workspace)
    owns_client = client is None
    active = (
        client
        if client is not None
        else ILinkClient(
            base_url=base_url, allow_insecure_local=allow_insecure_local_base
        )
    )
    try:
        try:
            qr = await active.get_qrcode(local_token_list=[])
        except ILinkError as exc:
            raise QRLoginError(str(exc)) from None
        if on_qr is not None:
            on_qr(qr)
        verify_code: str | None = None
        for _ in range(max_polls):
            try:
                data = await active.get_qrcode_status(qr.qrcode, verify_code)
            except ILinkError:
                await sleep(interval)
                continue
            status = str(data.get("status", ""))
            if on_status is not None:
                on_status(status)
            if status == "confirmed":
                token = data.get("bot_token")
                bot_id = data.get("ilink_bot_id")
                confirmed_base = data.get("baseurl") or base_url
                if (
                    not isinstance(token, str)
                    or not token
                    or not isinstance(bot_id, str)
                    or not bot_id
                ):
                    raise QRLoginError("登录响应缺少 bot token 或账号 ID")
                try:
                    confirmed_base = validate_base_url(
                        str(confirmed_base),
                        allow_insecure_local=allow_insecure_local_base,
                    )
                except ILinkError as exc:
                    raise QRLoginError(f"登录返回了不安全的 base URL: {exc}") from None
                account = WechatAccount(
                    bot_token=token,
                    ilink_bot_id=bot_id,
                    base_url=str(confirmed_base),
                    ilink_user_id=str(data.get("ilink_user_id", "")),
                )
                WechatState(state_dir).save_account(account)
                return account
            if status in ("wait", "scaned"):
                await sleep(interval)
                continue
            if status == "need_verifycode":
                if prompt_verify is None:
                    raise QRLoginError("需要验证码, 但当前入口没有交互输入")
                entered = prompt_verify("请输入手机显示的验证码: ")
                if isawaitable(entered):
                    entered = await entered
                verify_code = str(entered).strip()
                if not verify_code:
                    raise QRLoginError("未输入验证码")
                continue
            if status == "verify_code_blocked":
                raise QRLoginError("验证码失败次数过多, 请重新登录")
            if status == "expired":
                raise QRLoginError("二维码已过期, 请重新登录")
            if status == "scaned_but_redirect":
                host = data.get("redirect_host")
                if isinstance(host, str) and host:
                    if not owns_client:
                        raise QRLoginError(
                            "redirect requires an owned iLink client; "
                            "injected clients cannot be re-based"
                        )
                    base = redirect_base_url(host)
                    await active.close()
                    active = ILinkClient(
                        base_url=base,
                        allow_insecure_local=allow_insecure_local_base,
                    )
                await sleep(interval)
                continue
            if status == "binded_redirect":
                raise QRLoginError("该 Bot 已绑定到其他实例")
            await sleep(interval)
        raise QRLoginError("二维码登录超时")
    finally:
        if owns_client:
            await active.close()
