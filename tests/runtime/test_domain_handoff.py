"""Real shared-runtime chat -> development handoff and migration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from cairn.server.domain_migration import plan_migration
from tests.support.git import initialize_repository


def test_real_chat_to_development_creates_one_board_card_and_migrates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)

        class AdaptiveModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                names = {tool["function"]["name"] for tool in tools or ()}
                if "begin_development" in names:
                    if any(
                        message.role == "user"
                        and "Create the artifact" in (message.content or "")
                        for message in messages
                    ):
                        return LLMResponse(
                            tool_calls=[
                                ToolCall(
                                    id="begin",
                                    name="begin_development",
                                    arguments={"prompt": "Create the artifact"},
                                )
                            ],
                            usage=LLMUsage(input_tokens=10, output_tokens=5),
                        )
                    return LLMResponse(
                        content="chat reply",
                        usage=LLMUsage(input_tokens=10, output_tokens=5),
                    )
                return await super().generate(messages, tools)

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = AdaptiveModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        final: dict[str, Any] | None = None
        cards: tuple[Any, ...] = ()
        async with app.router.lifespan_context(app):
            first = await api.submit("Hello", "cli", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            assert app.state.runtime.list_tasks()[0].worktree is None

            current = await api.inspect(first["task_id"])
            await api.message(
                first["task_id"],
                "Create the artifact",
                source="cli",
                request_id="handoff-message",
                expected_sequence=current["sequence"],
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            final = await api.inspect(first["task_id"])
            assert final["status"] == "completed", final
            assert final["mode"] == "development"
            assert final["worktree"] is not None
            assert final["task_id"] == first["task_id"]

            board = app.state.runtime.board
            assert board is not None
            for _ in range(100):
                if board.store.load():
                    break
                await asyncio.sleep(0.02)
            cards = board.store.load()
            assert len(cards) == 1
            assert cards[0].task_id == final["task_id"]

        assert final is not None
        plan = plan_migration(
            tmp_path / "state",
            source,
            taskboard_path=tmp_path / "state" / "board" / "tasks.json",
        )
        assert len(plan.conversations) == 1
        assert len(plan.tasks) == 1
        assert len(plan.runs) == 1
        assert plan.tasks[0].task_id == final["task_id"]
        assert plan.tasks[0].worktree == final["worktree"]
        assert plan.tasks[0].metadata["legacy_mode"] == "development"

    asyncio.run(scenario())
