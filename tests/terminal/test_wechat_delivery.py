"""Server-owned delivery coordinator: one final WeChat reply per Run."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cairn.core.domain import RunStatus
from cairn.infra.wechat.state import WechatState
from cairn.terminal.wechat.delivery import WechatDeliveryCoordinator


class FakeConversations:
    def __init__(self, run: Any) -> None:
        self.run = run
        self.listeners: list[Any] = []

    def add_run_settled_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_run_settled_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)

    def get_run(self, run_id: str) -> Any:
        return self.run if run_id == self.run.run_id else None

    def get_wechat_conversation(self, _account_id: str, _sender_id: str) -> Any:
        return SimpleNamespace(conversation_id=self.run.conversation_id)

    def run_by_request(self, conversation_id: str, request_id: str) -> Any:
        if (
            conversation_id == self.run.conversation_id
            and request_id == self.run.request_id
        ):
            return self.run
        return None


class FakeWechat:
    def __init__(self, account_id: str | None = "bot-1") -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.account = account_id

    def account_id(self) -> str | None:
        return self.account

    async def deliver_for_account(
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
    ) -> bool:
        self.sent.append((sender, text, context_token or ""))
        return True


def _state_intent(state: WechatState, *, message_id: str = "message-1") -> str:
    state.record_received(
        "msg:1",
        sender="owner",
        context_token="context-token",
        text="hello",
    )
    return state.begin_delivery_intent(
        message_key="msg:1",
        message_id=message_id,
        request_id="request-1",
        account_id="bot-1",
        sender_id="owner",
        context_token="context-token",
        content="hello",
    )


def _run(
    *, status: RunStatus = RunStatus.COMPLETED, result: str = "final answer"
) -> Any:
    return SimpleNamespace(
        run_id="run-1",
        conversation_id="conv-1",
        request_id="request-1",
        status=status,
        result=result,
    )


def test_completed_run_is_delivered_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        run = _run()
        conversations: Any = FakeConversations(run)
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
            await asyncio.sleep(0.05)
            assert wechat.sent == [("owner", "final answer", "context-token")]
            row = state.get_delivery_intent(key)
            assert row is not None
            assert row["delivery_status"] == "sent"
            conversations.listeners[0](run)
            await asyncio.sleep(0.05)
            assert len(wechat.sent) == 1
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_late_binding_after_run_completion_still_delivers(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        run = _run()
        conversations: Any = FakeConversations(run)
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            # The Run settled before conversation_id/run_id were persisted.
            # The binding lookup recovers the conversation and request Run.
            conversations.listeners[0](run)
            await asyncio.sleep(0.05)
            assert len(wechat.sent) == 1
            row = state.get_delivery_intent(key)
            assert row is not None
            assert row["delivery_status"] == "sent"
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_started_intent_is_unconfirmed_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
        assert state.begin_delivery_attempt(key) is True
        conversations: Any = FakeConversations(_run())
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            await asyncio.sleep(0.05)
            assert wechat.sent == []
            row = state.get_delivery_intent(key)
            assert row is not None
            assert row["delivery_status"] == "unconfirmed"
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_failed_run_body_is_concise(tmp_path: Path) -> None:
    from cairn.terminal.wechat.delivery import _delivery_body

    run = _run(status=RunStatus.RECOVERY_REQUIRED)
    body = _delivery_body(run)
    assert "恢复核对" in body
    assert "run-" not in body


def test_pending_run_is_delivered_on_startup(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
        conversations: Any = FakeConversations(_run())
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            await asyncio.sleep(0.05)
            assert len(wechat.sent) == 1
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_unconfirmed_is_never_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
        assert state.begin_delivery_attempt(key) is True
        state.mark_delivery_unconfirmed(key)
        conversations: Any = FakeConversations(_run())
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            await asyncio.sleep(0.05)
            assert wechat.sent == []
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_missing_account_keeps_intent_pending_until_rescan(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
        conversations: Any = FakeConversations(_run())
        wechat: Any = FakeWechat(account_id=None)
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            await asyncio.sleep(0.05)
            row = state.get_delivery_intent(key)
            assert row is not None
            assert row["delivery_status"] == "pending"
            assert wechat.sent == []
            wechat.account = "bot-1"
            coordinator.rescan()
            await asyncio.sleep(0.05)
            assert len(wechat.sent) == 1
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_missing_conversation_id_is_recovered_from_binding(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = WechatState(tmp_path / "wechat")
        key = _state_intent(state)
        conversations: Any = FakeConversations(_run())
        wechat: Any = FakeWechat()
        coordinator = WechatDeliveryCoordinator(
            conversations=conversations, wechat=wechat, state=state
        )
        await coordinator.start()
        try:
            await asyncio.sleep(0.05)
            row = state.get_delivery_intent(key)
            assert row is not None
            assert row["conversation_id"] == "conv-1"
            assert row["run_id"] == "run-1"
            assert len(wechat.sent) == 1
        finally:
            await coordinator.close()

    asyncio.run(scenario())
