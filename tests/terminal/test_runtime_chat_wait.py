"""CLI RemoteChat must drain every Run event page before returning."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from cairn.agent.budget import RunBudget
from cairn.conversation.models import ConversationSource, RunStatus
from cairn.interfaces.cli.runtime_chat import RemoteChat
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


def test_remote_chat_posts_a_steer_to_the_running_run() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "run_id": "run-abc",
                "status": "running",
                "pending": 1,
            },
        )

    api = RuntimeClient(
        "http://127.0.0.1:8777",
        "zzzz",
        transport=httpx.MockTransport(handler),
    )
    remote = RemoteChat(api)
    remote.task = {"run_id": "run-abc", "cursor": 0}

    receipt = asyncio.run(remote.steer("new direction"))

    assert receipt["accepted"] is True
    assert seen == [
        ("POST", "/api/runtime/runs/run-abc/steer", {"prompt": "new direction"})
    ]


def test_remote_chat_wait_surfaces_steer_status_events(tmp_path: Path) -> None:
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
            initial_prompt="steering",
        )
        run = store.accept_run(
            conversation_id=conversation.conversation_id,
            prompt="steering",
            source=ConversationSource.CLI,
            request_id="steering-request",
        )
        store.append_run_event(run.run_id, "steer_accepted", {"text": "redirect"})
        store.append_run_event(run.run_id, "steer_applied", {"text": "redirect"})
        store.update_run(run.run_id, status=RunStatus.COMPLETED, result="done")
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        seen: list[tuple[str, object]] = []
        async with app.router.lifespan_context(app):
            remote = RemoteChat(api)
            remote.task = {
                "run_id": run.run_id,
                "cursor": 0,
                "status": "completed",
            }
            await remote.wait(
                on_event=lambda item: seen.append(
                    (item["kind"], item["data"].get("text"))
                )
            )
        assert seen == [
            ("steer_accepted", "redirect"),
            ("steer_applied", "redirect"),
        ]

    asyncio.run(scenario())


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
