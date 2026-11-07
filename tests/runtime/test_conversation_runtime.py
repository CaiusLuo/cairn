"""Online Conversation/Run execution through the real shared Runtime shape."""

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


class ChatModel(OfflineDevelopmentModel):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        user = [message.content for message in messages if message.role == "user"]
        return LLMResponse(
            content="reply:" + ((user[-1] or "") if user else ""),
            usage=LLMUsage(input_tokens=10, output_tokens=4),
        )


def test_conversation_runs_are_independent_and_do_not_create_tasks(
    tmp_path: Path,
) -> None:
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
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            assert conversation["conversation_id"].startswith("conv-")
            run_ids: list[str] = []
            for index in range(3):
                run = await api.send_conversation(
                    conversation["conversation_id"],
                    f"turn {index}",
                    source="cli",
                    request_id=f"turn-{index}",
                )
                run_ids.append(run["run_id"])
                for _ in range(100):
                    current = await api.get_run(run["run_id"])
                    if current["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.02)
                assert current["status"] == "completed", current
                assert current["result"] == f"reply:turn {index}"
            assert len(set(run_ids)) == 3
            detail = await api.get_conversation(conversation["conversation_id"])
            assert len(detail["runs"]) == 3
            history = await api.conversation_history(conversation["conversation_id"])
            assert [
                message["content"] for message in history if message["role"] == "user"
            ] == ["turn 0", "turn 1", "turn 2"]
            assert app.state.runtime.board is not None
            assert app.state.runtime.board.store.load() == ()

    asyncio.run(scenario())


def test_duplicate_request_id_returns_the_same_run(
    tmp_path: Path,
) -> None:
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
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            first = await api.send_conversation(
                conversation["conversation_id"],
                "once",
                source="cli",
                request_id="same-request",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            second = await api.send_conversation(
                conversation["conversation_id"],
                "once",
                source="cli",
                request_id="same-request",
            )
            assert first["run_id"] == second["run_id"]
            assert (
                len(
                    app.state.conversations.runs_for_conversation(
                        conversation["conversation_id"]
                    )
                )
                == 1
            )

    asyncio.run(scenario())


def test_read_only_tool_chat_creates_no_task_or_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)

        class ReadModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                del tools
                if not any(message.role == "tool" for message in messages):
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="read-1",
                                name="read_file",
                                arguments={"path": "tracked.txt"},
                            )
                        ],
                        usage=LLMUsage(input_tokens=10, output_tokens=2),
                    )
                return LLMResponse(
                    content="read complete",
                    usage=LLMUsage(input_tokens=10, output_tokens=2),
                )

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ReadModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation(
                "read tracked.txt", source="cli"
            )
            run = await api.send_conversation(
                conversation["conversation_id"],
                "read tracked.txt",
                source="cli",
                request_id="read-only",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert app.state.runtime.list_tasks() == []
            assert app.state.runtime.board is not None
            assert app.state.runtime.board.store.load() == ()
            history = await api.conversation_history(conversation["conversation_id"])
            assert any(message["role"] == "tool" for message in history)
            assert not any(
                path.is_file() for path in (tmp_path / "worktrees").rglob("*")
            )

    asyncio.run(scenario())


def test_second_request_while_run_is_active_returns_conflict(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        release = asyncio.Event()

        class SlowModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                del messages, tools
                await release.wait()
                return LLMResponse(
                    content="slow complete",
                    usage=LLMUsage(input_tokens=1, output_tokens=1),
                )

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: SlowModel()
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("slow", source="cli")
            first = await api.send_conversation(
                conversation["conversation_id"],
                "first",
                source="cli",
                request_id="first",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] == "running":
                    break
                await asyncio.sleep(0.02)
            try:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "second",
                    source="web",
                    request_id="second",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
            else:
                raise AssertionError("second request should have been rejected")
            release.set()
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

    asyncio.run(scenario())
