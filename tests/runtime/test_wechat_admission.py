"""WeChat admission outcomes stay separate from delivery outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.infra.wechat.state import WechatState
from cairn.terminal.wechat.runtime import (
    WechatConversationAdapter,
    wechat_request_id,
)


class BusyClient:
    def __init__(self, code: str) -> None:
        self.code = code

    async def ensure_wechat_conversation(self, *_args: Any) -> dict[str, Any]:
        return {"conversation_id": "conv-1"}

    async def send_conversation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        request = httpx.Request("POST", "http://test/conversations/conv-1/messages")
        response = httpx.Response(409, json={"error": self.code}, request=request)
        raise httpx.HTTPStatusError("conflict", request=request, response=response)


class TimeoutClient(BusyClient):
    async def send_conversation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        request = httpx.Request("POST", "http://test/conversations/conv-1/messages")
        raise httpx.ReadTimeout("timeout", request=request)

    async def find_run_by_request(
        self, _conversation_id: str, _request_id: str
    ) -> dict[str, Any]:
        return {"run_id": "run-1", "status": "completed", "result": "recovered"}

    async def run_events(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"events": [], "cursor": 1}

    async def get_run(self, _run_id: str) -> dict[str, Any]:
        return {
            "run_id": "run-1",
            "status": "completed",
            "result": "recovered",
            "cursor": 1,
        }


def _prepare(state: WechatState, text: str = "hello") -> tuple[str, str]:
    request_id = wechat_request_id("bot-1", "owner", "message-1")
    key = state.begin_delivery_intent(
        message_key="msg-1",
        message_id="message-1",
        request_id=request_id,
        account_id="bot-1",
        sender_id="owner",
        context_token="context",
        content=text,
    )
    return key, request_id


@pytest.mark.parametrize(
    "code", ["conversation_busy", "conversation_recovery_required", "invalid_request"]
)
def test_http_409_changes_admission_not_delivery(tmp_path: Path, code: str) -> None:
    state = WechatState(tmp_path / "wechat")
    key, request_id = _prepare(state)
    adapter = WechatConversationAdapter(BusyClient(code), delivery_state=state)  # type: ignore[arg-type]
    answer = asyncio_run(
        adapter.reply(
            "hello",
            message_id="message-1",
            sender="owner",
            account="bot-1",
            delivery_key=key,
            request_id=request_id,
        )
    )
    assert answer
    row = state.get_delivery_intent(key)
    assert row is not None
    assert row["admission_status"] == "rejected"
    assert row["delivery_status"] == "pending"


def test_timeout_recovers_original_run_from_request_id(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key, request_id = _prepare(state)
    adapter = WechatConversationAdapter(TimeoutClient(""), delivery_state=state)  # type: ignore[arg-type]
    answer = asyncio_run(
        adapter.reply(
            "hello",
            message_id="message-1",
            sender="owner",
            account="bot-1",
            delivery_key=key,
            request_id=request_id,
        )
    )
    assert answer == "recovered"
    row = state.get_delivery_intent(key)
    assert row is not None
    assert row["admission_status"] == "accepted"
    assert row["run_id"] == "run-1"
    assert row["delivery_status"] == "pending"


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)
