"""UX-A WeChat ordinary chat keeps technical facts out of normal replies."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from cairn.core.budget import RunBudget
from cairn.infra.runtime_client import RuntimeClient
from cairn.server import runtime_app
from cairn.terminal.wechat.runtime import WechatRuntimeAdapter
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
        adapter = WechatRuntimeAdapter(api)
        async with app.router.lifespan_context(app):
            answer = await adapter.reply(
                "Hello", message_id="wechat-ux-1", conversation_key="wechat:owner"
            )
            assert answer
            assert "task-" not in answer
            assert "Worktree" not in answer
            assert "步骤:" not in answer
            assert "Token:" not in answer
            assert len(app.state.runtime.list_tasks()) == 1
            task = app.state.runtime.list_tasks()[0]
            assert task.worktree is None

    asyncio.run(scenario())
