"""Real Chat -> begin_development handoff through the legacy development path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.infra.runtime_client import RuntimeClient
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.server import runtime_app
from tests.support.git import initialize_repository


class HandoffModel(OfflineDevelopmentModel):
    _asked = False

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        names = {tool["function"]["name"] for tool in tools or ()}
        if (
            "begin_development" in names
            and any(
                message.role == "user"
                and "Create the development artifact" in (message.content or "")
                for message in messages
            )
            and not type(self)._asked
        ):
            type(self)._asked = True
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="begin-development",
                        name="begin_development",
                        arguments={"prompt": "Create the development artifact"},
                    )
                ],
                usage=LLMUsage(input_tokens=10, output_tokens=3),
            )
        return await super().generate(messages, tools)


def test_chat_begin_development_handoff_preserves_conversation_and_creates_one_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        HandoffModel._asked = False
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
        app.state.execution.llm_factory = HandoffModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("Hello", source="cli")
            first = await api.send_conversation(
                conversation["conversation_id"],
                "Hello",
                source="cli",
                request_id="handoff-chat",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

            second = await api.send_conversation(
                conversation["conversation_id"],
                "Create the development artifact",
                source="cli",
                request_id="handoff-dev",
            )
            for _ in range(200):
                current = await api.get_run(second["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            assert current["status"] == "completed", current
            assert current["task_id"] is not None

            tasks = app.state.runtime.list_tasks()
            assert len(tasks) == 1
            legacy_task = tasks[0]
            assert legacy_task.worktree is not None
            assert app.state.runtime.board is not None
            cards = app.state.runtime.board.store.load()
            assert len(cards) == 1
            assert cards[0].task_id == legacy_task.task_id
            assert len(app.state.conversations.store.list_tasks()) == 1
            domain_task = app.state.conversations.store.list_tasks()[0]
            assert domain_task.task_id == legacy_task.task_id
            assert domain_task.conversation_id == conversation["conversation_id"]
            history = await api.conversation_history(conversation["conversation_id"])
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "Hello",
                "Create the development artifact",
            ]

    asyncio.run(scenario())
