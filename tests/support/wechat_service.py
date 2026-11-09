"""Shared offline WeChat service test fixtures."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from cairn.development.execution.headless import HeadlessResult, HeadlessStatus
from cairn.integrations.wechat.ilink import ILinkClient
from cairn.integrations.wechat.serve import ConversationDecision, ServeConfig
from cairn.integrations.wechat.state import WechatAccount
from tests.support.fake_ilink import FakeILink

TOKEN = "secret-bot-token-do-not-leak"


async def _task_only_classifier(_text: str) -> ConversationDecision:
    return ConversationDecision.TASK


def _account(fake: FakeILink) -> WechatAccount:
    return WechatAccount(
        bot_token=TOKEN,
        ilink_bot_id="bot-1",
        base_url=fake.url,
        ilink_user_id="owner",
    )


def _msg(
    text: str | None,
    *,
    sender: str = "u1",
    context: str = "ctx-1",
    mid: int | None = 1,
    item_type: int = 1,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_type": 1,
        "from_user_id": sender,
        "context_token": context,
        "message_id": mid,
        "item_list": [
            {"type": item_type, "text_item": {"text": text or ""}}
            if item_type == 1
            else {"type": item_type}
        ],
    }
    if mid is None:
        message.pop("message_id")
    return message


def _config(
    fake: FakeILink,
    workspace: Path,
    state_dir: Path,
    *,
    max_polls: int | None = 1,
) -> ServeConfig:
    return ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=_account(fake),
        approval=None,
        allowed_senders=frozenset({"u1"}),
        max_polls=max_polls,
        poll_interval=0.0,
    )


def _client(fake: FakeILink) -> ILinkClient:
    return ILinkClient(base_url=fake.url, bot_token=TOKEN, allow_insecure_local=True)


def _completed(
    workspace: Path,
) -> Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]:
    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            verified=True,
            workspace=str(workspace),
            final_response="done: " + prompt,
        )

    return executor


async def _noop(_delay: float) -> None:
    return None
