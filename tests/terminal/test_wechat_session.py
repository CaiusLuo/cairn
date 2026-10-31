"""In-session /wx: ASCII QR login then background service start/stop."""

import asyncio
from pathlib import Path

import pytest

from cairn.terminal.headless import HeadlessResult, HeadlessStatus
from cairn.terminal.wechat.qr import render_ascii_qr
from cairn.terminal.wechat.session import WechatSessionController
from tests.support.fake_ilink import FakeILink
from tests.support.runtime import (
    SequenceLLM,  # noqa: F401  (typing/import compatibility)
)


def test_render_ascii_qr_is_block_text_without_ansi() -> None:
    rendered = render_ascii_qr("https://example.test/login")
    assert "██" in rendered
    assert "\x1b[" not in rendered
    assert len(rendered.splitlines()) > 5


def test_wx_logs_in_starts_and_stops_in_session(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    emits: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    with FakeILink() as fake:
        fake.qr_statuses = [{"status": "confirmed"}]
        fake.confirm = {
            "bot_token": "token-session-test",
            "ilink_bot_id": "bot-session",
            "baseurl": fake.url,
            "ilink_user_id": "owner",
        }
        from cairn.infra.wechat.ilink import ILinkClient

        login_client = ILinkClient(
            base_url=fake.url, bot_token=None, allow_insecure_local=True
        )
        controller = WechatSessionController(
            workspace=workspace,
            state_dir=state_dir,
            task_executor=executor,
            emit=emits.append,
            prompt_verify=lambda _m: "",
            allow_insecure_local_http=True,
            login_client=login_client,
        )

        async def scenario() -> None:
            started = await controller.command("")
            assert "微信登录已开始" in getattr(started, "message", "")
            for _ in range(200):
                if controller._task is not None and not controller._task.done():
                    break
                await asyncio.sleep(0.01)
            assert any("请使用微信扫描" in line for line in emits)
            assert any("█" in line for line in emits)
            assert (state_dir / "account.json").exists()

            status = await controller.command("status")
            assert "已登录" in getattr(status, "message", "")
            assert "运行中" in getattr(status, "message", "")
            stopped = await controller.command("stop")
            assert "已停止" in getattr(stopped, "message", "")
            await login_client.close()

        asyncio.run(scenario())


def test_wx_login_waiting_can_be_stopped(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    emits: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    with FakeILink() as fake:
        fake.qr_statuses = [{"status": "wait"} for _ in range(20)]
        from cairn.infra.wechat.ilink import ILinkClient

        login_client = ILinkClient(
            base_url=fake.url, bot_token=None, allow_insecure_local=True
        )
        controller = WechatSessionController(
            workspace=workspace,
            state_dir=state_dir,
            task_executor=executor,
            emit=emits.append,
            allow_insecure_local_http=True,
            login_client=login_client,
        )

        async def scenario() -> None:
            started = await controller.command("")
            assert "微信登录已开始" in getattr(started, "message", "")
            status = await controller.command("status")
            assert "等待扫码" in getattr(status, "message", "")
            stopped = await controller.command("stop")
            assert "已取消" in getattr(stopped, "message", "")
            assert controller._login_task is None
            await login_client.close()

        asyncio.run(scenario())
    assert not (state_dir / "account.json").exists()


def test_qr_refuses_to_clip_on_narrow_terminal() -> None:
    from cairn.terminal.wechat.qr import render_ascii_qr

    with pytest.raises(ValueError):
        render_ascii_qr("https://example.test/login", max_width=10)


def test_wx_stop_cancels_running_service(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import cairn.terminal.wechat.session as session_module
    from cairn.infra.wechat.state import WechatAccount

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    account = WechatAccount(
        bot_token="token-stop",
        ilink_bot_id="bot-stop",
        base_url="https://example.test",
        ilink_user_id="owner",
    )
    from cairn.infra.wechat.state import WechatState

    WechatState(state_dir).save_account(account)

    async def fake_serve(**_kwargs: object) -> object:
        await asyncio.Event().wait()
        return object()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    monkeypatch.setattr(session_module, "serve_wechat", fake_serve)
    controller = WechatSessionController(
        workspace=workspace,
        state_dir=state_dir,
        task_executor=executor,
        emit=lambda _line: None,
    )

    async def scenario() -> None:
        await controller.command("")
        status = await controller.command("status")
        assert "运行中" in getattr(status, "message", "")
        stopped = await controller.command("stop")
        assert "已停止" in getattr(stopped, "message", "")
        assert controller._task is None

    asyncio.run(scenario())


def test_deliver_uses_latest_saved_context_for_proactive_push(tmp_path: Path) -> None:
    from cairn.infra.wechat.state import WechatAccount, WechatState

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    with FakeILink() as fake:
        state.save_account(
            WechatAccount(
                bot_token="token-delivery",
                ilink_bot_id="bot-delivery",
                base_url=fake.url,
                ilink_user_id="owner",
            )
        )
        state.load()["messages"]["msg:1"] = {
            "sender": "owner",
            "context_token": "ctx-delivery",
            "updated": 1.0,
        }
        state.flush()

        async def executor(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            return HeadlessResult(
                task_id=task_id,
                status=HeadlessStatus.COMPLETED,
                workspace=str(tmp_path),
            )

        controller = WechatSessionController(
            workspace=workspace,
            state_dir=state_dir,
            task_executor=executor,
            emit=lambda _line: None,
            allow_insecure_local_http=True,
        )
        assert asyncio.run(controller.deliver("owner", "scheduled result")) is True

    assert fake.sent
    assert fake.sent[-1]["context_token"] == "ctx-delivery"


def test_wx_allow_controls_scheduler_recipient_and_delivery(tmp_path: Path) -> None:
    from cairn.infra.wechat.state import WechatAccount, WechatState

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    WechatState(state_dir).save_account(
        WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    controller = WechatSessionController(
        workspace=workspace,
        state_dir=state_dir,
        task_executor=executor,
        emit=lambda _line: None,
    )
    assert controller.recipient_id() == "owner"
    asyncio.run(controller.command("allow another-person"))
    # Allowing incoming tasks must not change the scheduled delivery owner.
    assert controller.recipient_id() == "owner"
    assert not asyncio.run(
        controller.deliver_for_account("another-person", "private result", "bot-test")
    )


def test_wx_allow_adds_sender_without_revoking_login_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.terminal.wechat.session as session_module
    from cairn.infra.wechat.state import WechatAccount, WechatState
    from cairn.terminal.wechat.serve import ServeConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    WechatState(state_dir).save_account(
        WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    allowed: list[frozenset[str]] = []

    async def fake_serve(
        *, config: ServeConfig, stop_event: asyncio.Event, **kwargs: object
    ) -> None:
        allowed.append(config.allowed_senders)
        await stop_event.wait()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    monkeypatch.setattr(session_module, "serve_wechat", fake_serve)
    controller = WechatSessionController(
        workspace=workspace,
        state_dir=state_dir,
        task_executor=executor,
        emit=lambda _line: None,
    )

    async def scenario() -> None:
        await controller.command("allow another-person")
        await controller.command("")
        await asyncio.sleep(0)
        assert allowed == [frozenset({"owner", "another-person"})]
        await controller.close()

    asyncio.run(scenario())
