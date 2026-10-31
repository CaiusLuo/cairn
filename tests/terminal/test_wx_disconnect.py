from __future__ import annotations

import asyncio
from pathlib import Path

from cairn.terminal.wechat.session import WechatSessionController


def test_wx_help_lists_disconnect_and_does_not_start(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    controller = WechatSessionController(
        workspace=workspace,
        state_dir=tmp_path / "state",
        task_executor=lambda *a: None,  # type: ignore[arg-type]
        emit=lambda _text: None,
    )
    for command in ("help", "--help"):
        action = asyncio.run(controller.command(command))
        message = getattr(action, "message", "")
        assert "/wx disconnect" in message
        assert "/wx stop" in message
    assert controller._task is None


def test_wx_disconnect_clears_account_and_allows_fresh_login(tmp_path: Path) -> None:
    from cairn.infra.wechat.state import WechatAccount, WechatState

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.save_account(
        WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-old",
            base_url="https://example.test",
            ilink_user_id="owner-old",
        )
    )
    state.set_cursor("old-cursor")
    controller = WechatSessionController(
        workspace=workspace,
        state_dir=state_dir,
        task_executor=lambda *a: None,  # type: ignore[arg-type]
        emit=lambda _text: None,
    )
    action = asyncio.run(controller.command("disconnect"))
    assert "重新扫码" in getattr(action, "message", "")
    assert not (state_dir / "account.json").exists()
    assert not (state_dir / "state.json").exists()
    status = asyncio.run(controller.command("status"))
    assert "未登录" in getattr(status, "message", "")
    assert controller.recipient_id() is None
    assert controller.account_id() is None
