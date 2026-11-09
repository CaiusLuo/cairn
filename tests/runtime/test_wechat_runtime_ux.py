"""UX-A WeChat ordinary chat keeps technical facts out of normal replies."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from cairn.agent.budget import RunBudget
from cairn.integrations.wechat.runtime import WechatConversationAdapter
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


def test_wechat_normal_chat_returns_only_final_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        adapter = WechatConversationAdapter(api)
        async with app.router.lifespan_context(app):
            answer = await adapter.reply(
                "Hello",
                message_id="wechat-ux-1",
                sender="owner",
                account="bot-1",
            )
            assert answer
            assert "task-" not in answer
            assert "Worktree" not in answer
            assert "步骤:" not in answer
            assert "Token:" not in answer
            assert app.state.runtime.list_tasks() == []
            assert len(app.state.conversations.list_conversations()) == 1

    asyncio.run(scenario())
