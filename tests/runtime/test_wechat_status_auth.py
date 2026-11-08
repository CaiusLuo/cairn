"""WeChat status commands stay scoped to the sender's Conversation."""

from __future__ import annotations

import asyncio
from typing import Any

from cairn.terminal.wechat.runtime import WechatConversationAdapter


class FakeClient:
    def __init__(
        self,
        *,
        conversation_id: str = "conv-owner-a",
        run_conversation_id: str = "conv-owner-a",
        task_recipient: str = "owner-a",
    ) -> None:
        self.conversation_id = conversation_id
        self.run_conversation_id = run_conversation_id
        self.task_recipient = task_recipient

    async def get_wechat_conversation(
        self, _account: str, _sender: str
    ) -> dict[str, Any]:
        return {"conversation_id": self.conversation_id}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "conversation_id": self.run_conversation_id,
            "status": "completed",
            "result": "private result",
        }

    async def inspect(self, task_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": "completed",
            "delivery_recipient": self.task_recipient,
            "delivery_account": "bot-1",
            "result": "private task result",
        }


def _reply(adapter: WechatConversationAdapter, text: str) -> str:
    return asyncio.run(
        adapter.reply(
            text,
            message_id="status-1",
            sender="owner-a",
            account="bot-1",
        )
    )


def test_status_rejects_run_from_another_sender() -> None:
    adapter = WechatConversationAdapter(
        FakeClient(run_conversation_id="conv-owner-b")  # type: ignore[arg-type]
    )
    answer = _reply(adapter, "/status run-other")
    assert "private result" not in answer
    assert "未找到" in answer


def test_status_rejects_cli_or_web_run() -> None:
    adapter = WechatConversationAdapter(
        FakeClient(run_conversation_id="conv-cli")  # type: ignore[arg-type]
    )
    answer = _reply(adapter, "/status run-cli")
    assert "private result" not in answer
    assert "未找到" in answer


def test_task_query_rejects_task_owned_by_another_sender() -> None:
    adapter = WechatConversationAdapter(
        FakeClient(task_recipient="owner-b")  # type: ignore[arg-type]
    )
    answer = _reply(adapter, "/task task-other")
    assert "private task result" not in answer
    assert "未找到" in answer
