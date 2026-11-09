"""CLI RemoteChat must drain every Run event page before returning."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from cairn.conversation.models import ConversationSource, RunStatus
from cairn.core.budget import RunBudget
from cairn.infra.runtime_client import RuntimeClient
from cairn.server import runtime_app
from cairn.terminal.runtime_chat import RemoteChat
from tests.support.git import initialize_repository


def test_remote_chat_wait_drains_550_events_in_order(tmp_path: Path) -> None:
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
        store = app.state.conversations.store
        conversation = store.create_conversation(
            workspace=source,
            source=ConversationSource.CLI,
            initial_prompt="paging",
        )
        run = store.accept_run(
            conversation_id=conversation.conversation_id,
            prompt="paging",
            source=ConversationSource.CLI,
            request_id="paging-request",
        )
        for index in range(550):
            store.append_run_event(run.run_id, "agent_step", {"index": index})
        store.update_run(run.run_id, status=RunStatus.COMPLETED, result="done")
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        seen: list[int] = []
        async with app.router.lifespan_context(app):
            remote = RemoteChat(api)
            remote.task = {
                "run_id": run.run_id,
                "cursor": 0,
                "status": "completed",
            }
            final = await remote.wait(
                lambda event: seen.append(int(event.data["index"]))
            )
            assert final["cursor"] == 550
            assert seen == list(range(550))

    asyncio.run(scenario())
