"""Real shared chat path can retrieve saved owner knowledge on demand."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.knowledge.store import KnowledgeStore
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


def test_owner_chat_reads_saved_clip_through_registered_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        state = tmp_path / "state"
        clip, _ = KnowledgeStore(state / "knowledge").create(
            "CAIRN-KNOWLEDGE-SENTINEL: blue-shaped stone",
            tags=["private-reference"],
        )

        class KnowledgeModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                names = {tool["function"]["name"] for tool in tools or ()}
                assert {"knowledge_list", "knowledge_search", "knowledge_get"} <= names
                tool_messages = [
                    message.content or ""
                    for message in messages
                    if message.role == "tool"
                ]
                if tool_messages:
                    assert any(
                        "CAIRN-KNOWLEDGE-SENTINEL" in text
                        for text in tool_messages
                    )
                    return LLMResponse(
                        content=f"Retrieved from [{clip.clip_id}]: blue-shaped stone",
                        usage=LLMUsage(input_tokens=12, output_tokens=12),
                    )
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-knowledge",
                            name="knowledge_get",
                            arguments={"clip_id": clip.clip_id},
                        )
                    ],
                    usage=LLMUsage(input_tokens=12, output_tokens=12),
                )

        app = runtime_app(
            source,
            state,
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="knowledge-test-token",
        )
        app.state.execution.llm_factory = KnowledgeModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "knowledge-test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation(
                "知识库里有什么？", source="cli"
            )
            run = await api.send_conversation(
                conversation["conversation_id"],
                "请读取我保存的知识片段并说明来源",
                source="cli",
                request_id="knowledge-chat-lookup",
            )
            await asyncio.wait_for(app.state.conversations.queue.join(), 30)
            result = await api.get_run(run["run_id"])
            assert result["status"] == "completed", result
            assert clip.clip_id in result["result"]
            history = await api.conversation_history(conversation["conversation_id"])
            assert any(
                message["role"] == "tool"
                and "CAIRN-KNOWLEDGE-SENTINEL" in str(message["content"])
                for message in history
            )

    asyncio.run(scenario())
