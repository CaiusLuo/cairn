"""Protocol-level Conversation API safety tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.models import ConversationSource, RunStatus
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class ChatModel(OfflineDevelopmentModel):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        return LLMResponse(
            content="reply", usage=LLMUsage(input_tokens=1, output_tokens=1)
        )


class SlowModel(OfflineDevelopmentModel):
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del messages, tools
        await self.release.wait()
        return LLMResponse(
            content="slow", usage=LLMUsage(input_tokens=1, output_tokens=1)
        )


def _app(tmp_path: Path) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )


def test_conversation_create_request_id_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            first = await api.create_conversation(
                "same", source="cli", request_id="conversation-request"
            )
            second = await api.create_conversation(
                "same", source="cli", request_id="conversation-request"
            )
            assert first["conversation_id"] == second["conversation_id"]
            try:
                await api.create_conversation(
                    "different",
                    source="cli",
                    request_id="conversation-request",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
            else:
                raise AssertionError("different create payload must be rejected")
            assert len(app.state.conversations.list_conversations()) == 1

    asyncio.run(scenario())


def test_cancel_is_post_only_and_get_never_mutates(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        slow = SlowModel()
        app.state.execution.llm_factory = lambda: slow
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("cancel", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "cancel",
                source="cli",
                request_id="cancel-request",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] == "running":
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "running"
            try:
                await api.request("GET", f"runs/{run['run_id']}/cancel")
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 405
            else:
                raise AssertionError("GET cancel must not be allowed")
            assert (await api.get_run(run["run_id"]))["status"] == "running"
            cancelled = await api.cancel_run(run["run_id"])
            assert cancelled["status"] == "cancelled"
            assert (await api.get_run(run["run_id"]))["status"] == "cancelled"

    asyncio.run(scenario())


def test_recovery_required_blocks_new_ordinary_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("recovery", source="cli")
            run = app.state.conversations.store.accept_run(
                conversation_id=conversation["conversation_id"],
                prompt="old",
                source=ConversationSource.CLI,
                request_id="old-request",
            )
            app.state.conversations.store.update_run(
                run.run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="effects_require_host_review",
            )
            try:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "new",
                    source="cli",
                    request_id="new-request",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
                assert (
                    error.response.json()["error"] == "conversation_recovery_required"
                )
            else:
                raise AssertionError("recovery must block a new ordinary Run")

    asyncio.run(scenario())
