"""Legacy WeChat owner scope at the real Conversation/Run boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.models import ConversationSource, Run, RunStatus
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.knowledge.models import Clip
from cairn.knowledge.store import KnowledgeStore
from cairn.server.bootstrap import runtime_app
from tests.support.git import initialize_repository

TOKEN = "offline-scope-token"
USAGE = LLMUsage(input_tokens=12, output_tokens=12)
PRIVATE_TOOLS = {
    "knowledge_list",
    "knowledge_search",
    "knowledge_get",
    "memory_search",
    "memory_remember",
    "memory_forget",
    "memory_correct",
    "schedule_create",
    "schedule_list",
    "schedule_manage",
    "read_skill",
    "read_skill_resource",
}
MEMORY_MARKER = "OWNER-MEMORY-MARKER"


class ScopeModel(OfflineDevelopmentModel):
    def __init__(
        self,
        *,
        expected: set[str],
        forbidden: set[str],
        answer: str,
        attempt_clip_id: str | None = None,
    ) -> None:
        self.expected = expected
        self.forbidden = forbidden
        self.answer = answer
        self.attempt_clip_id = attempt_clip_id
        self.turns: list[set[str]] = []
        self.messages: list[list[Message]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        names = {item["function"]["name"] for item in tools or ()}
        self.turns.append(names)
        self.messages.append(list(messages))
        assert self.expected <= names, f"missing tools: {self.expected - names}"
        assert not (self.forbidden & names), f"leaked tools: {self.forbidden & names}"
        discussed = [message for message in messages if message.role == "tool"]
        if self.attempt_clip_id is not None and not discussed:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="exact-clip-attempt",
                        name="knowledge_get",
                        arguments={"clip_id": self.attempt_clip_id},
                    )
                ],
                usage=USAGE,
            )
        return LLMResponse(content=self.answer, usage=USAGE)


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    state = tmp_path / "state"
    WechatState(state / "wechat").save_account(
        WechatAccount(
            bot_token="offline-wechat-token",
            ilink_bot_id="bot-1",
            base_url="http://127.0.0.1:1",
            ilink_user_id="owner",
        )
    )
    return source, state


def _app(source: Path, state: Path) -> Any:
    return runtime_app(
        source,
        state,
        source.parent / "worktrees",
        budget=RunBudget(8),
        token=TOKEN,
    )


def _clip(state: Path, content: str) -> Clip:
    clip, _ = KnowledgeStore(state / "knowledge").create(content)
    return clip


async def _run(
    app: Any,
    model: ScopeModel,
    conversation_id: str,
    request_id: str,
) -> tuple[Run, list[dict[str, Any]]]:
    app.state.execution.llm_factory = lambda: model
    run = app.state.conversations.submit(
        conversation_id,
        "请读取知识库并回答",
        ConversationSource.WECHAT,
        request_id,
    )
    await asyncio.wait_for(app.state.conversations.queue.join(), 30)
    final = app.state.conversations.get_run(run.run_id)
    assert final is not None
    assert final.status is RunStatus.COMPLETED, final
    return final, app.state.conversations.history(conversation_id)


def _contains(messages: list[list[Message]], marker: str) -> bool:
    return any(
        marker in (message.content or "") for turn in messages for message in turn
    )


def test_owner_has_private_capabilities_and_trusted_sender_has_none(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, state = _prepare(tmp_path)
        clip = _clip(state, "WECHAT-PRIVATE: owner-only fact")
        app = _app(source, state)
        app.state.controls.memory.store.write_memory_md(MEMORY_MARKER)
        async with app.router.lifespan_context(app):
            owner_conversation = app.state.conversations.ensure_wechat_conversation(
                "bot-1", "owner", "hello"
            )
            owner_model = ScopeModel(
                expected={
                    "knowledge_get",
                    "memory_search",
                    "memory_remember",
                    "schedule_create",
                    "read_skill",
                    "begin_development",
                },
                forbidden=set(),
                answer=f"Owner read {clip.clip_id}",
            )
            owner_final, _ = await _run(
                app, owner_model, owner_conversation.conversation_id, "owner-scope"
            )
            assert clip.clip_id in (owner_final.result or "")
            assert _contains(owner_model.messages, MEMORY_MARKER)

            guest_conversation = app.state.conversations.ensure_wechat_conversation(
                "bot-1", "trusted-guest", "hello"
            )
            guest_model = ScopeModel(
                expected={"read_file", "glob", "grep"},
                forbidden=PRIVATE_TOOLS | {"begin_development"},
                answer="无权读取私人知识片段。",
                attempt_clip_id=clip.clip_id,
            )
            guest_final, guest_history = await _run(
                app, guest_model, guest_conversation.conversation_id, "guest-scope"
            )
            assert "无权" in (guest_final.result or "")
            assert not _contains(guest_model.messages, MEMORY_MARKER)
            serialized = json.dumps(guest_history, ensure_ascii=False)
            assert clip.content not in serialized
            assert MEMORY_MARKER not in serialized

    asyncio.run(scenario())


def test_wechat_owner_binding_survives_restart_and_account_switch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, state = _prepare(tmp_path)
        clip = _clip(state, "WECHAT-RESTART: durable owner fact")
        first = _app(source, state)
        async with first.router.lifespan_context(first):
            owner_conversation = first.state.conversations.ensure_wechat_conversation(
                "bot-1", "owner", "hello"
            )
            guest_conversation = first.state.conversations.ensure_wechat_conversation(
                "bot-1", "trusted-guest", "hello"
            )
            owner_id = owner_conversation.conversation_id
            guest_id = guest_conversation.conversation_id

        reopened = _app(source, state)
        async with reopened.router.lifespan_context(reopened):
            owner_model = ScopeModel(
                expected={"knowledge_get", "memory_search", "schedule_create"},
                forbidden=set(),
                answer=f"After restart {clip.clip_id}",
            )
            owner_final, _ = await _run(
                reopened, owner_model, owner_id, "owner-after-restart"
            )
            assert clip.clip_id in (owner_final.result or "")

            guest_model = ScopeModel(
                expected={"read_file"},
                forbidden=PRIVATE_TOOLS | {"begin_development"},
                answer="Restart does not grant access.",
            )
            guest_final, guest_history = await _run(
                reopened, guest_model, guest_id, "guest-after-restart"
            )
            assert "does not grant access" in (guest_final.result or "")
            assert clip.content not in json.dumps(guest_history, ensure_ascii=False)

        WechatState(state / "wechat").save_account(
            WechatAccount(
                bot_token="offline-wechat-token",
                ilink_bot_id="bot-2",
                base_url="http://127.0.0.1:1",
                ilink_user_id="owner",
            )
        )
        switched = _app(source, state)
        async with switched.router.lifespan_context(switched):
            switched_model = ScopeModel(
                expected={"read_file"},
                forbidden=PRIVATE_TOOLS | {"begin_development"},
                answer="Account changed; owner authorization not inherited.",
            )
            switched_final, switched_history = await _run(
                switched, switched_model, owner_id, "owner-after-account-switch"
            )
            assert "not inherited" in (switched_final.result or "")
            assert clip.content not in json.dumps(switched_history, ensure_ascii=False)

    asyncio.run(scenario())
