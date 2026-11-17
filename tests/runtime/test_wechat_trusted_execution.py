"""Trusted WeChat senders cannot cross the execution boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.development.catalogue import RuntimeTask
from cairn.development.execution.headless import HeadlessResult, HeadlessStatus
from cairn.integrations.wechat import serve as serve_module
from cairn.integrations.wechat.runtime import (
    WechatConversationAdapter,
    WechatRuntimeAdapter,
)
from cairn.integrations.wechat.serve import (
    ServeConfig,
    default_conversation_executor,
    default_task_executor,
)
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.memory.store import PersonalMemoryStore
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.wechat_service import TOKEN as WECHAT_TOKEN

RUNTIME_TOKEN = "test-runtime-token"
MEMORY_MARKER = "OWNER-MEMORY-MARKER"


def _account() -> WechatAccount:
    return WechatAccount(
        bot_token=WECHAT_TOKEN,
        ilink_bot_id="bot-1",
        base_url="https://example.test",
        ilink_user_id="owner",
    )


def _build(tmp_path: Path) -> Any:
    return runtime_app(
        tmp_path / "source",
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token=RUNTIME_TOKEN,
    )


def _prepare(tmp_path: Path) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    WechatState(tmp_path / "state" / "wechat").save_account(_account())
    return _build(tmp_path)


def _client(app: Any) -> RuntimeClient:
    return RuntimeClient(
        "http://127.0.0.1:8777",
        RUNTIME_TOKEN,
        transport=httpx.ASGITransport(app=app),
    )


def test_wechat_task_creation_is_owner_only_and_existing_tasks_keep_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _prepare(tmp_path)
        async with app.router.lifespan_context(app):
            controls = app.state.controls
            controls.state = controls.state.model_copy(
                update={"trusted_senders": ("trusted",)}
            )
            api = _client(app)
            legacy = WechatRuntimeAdapter(api)

            # Server admission rejects a trusted sender even when the adapter
            # is bypassed entirely.
            with pytest.raises(httpx.HTTPStatusError) as trusted:
                await api.submit(
                    "create a trusted task",
                    "wechat",
                    request_id="trusted-create",
                    delivery_recipient="trusted",
                    delivery_account="bot-1",
                )
            assert trusted.value.response.status_code == 403
            assert trusted.value.response.json()["error"] == "task_submission_forbidden"
            assert app.state.runtime.list_tasks() == []

            answer = await legacy.reply(
                "/task create a trusted task",
                message_id="trusted-message",
                sender="trusted",
                account="bot-1",
            )
            assert "主人" in answer
            assert app.state.runtime.list_tasks() == []

            # An unknown sender still fails the existing delivery check.
            with pytest.raises(httpx.HTTPStatusError) as unknown:
                await api.submit(
                    "create an unknown task",
                    "wechat",
                    request_id="unknown-create",
                    delivery_recipient="stranger",
                    delivery_account="bot-1",
                )
            assert unknown.value.response.status_code == 409
            assert app.state.runtime.list_tasks() == []

            reply = await legacy.reply(
                "/task create an owner artifact",
                message_id="owner-message",
                sender="owner",
                account="bot-1",
            )
            assert "task-" in reply
            tasks = app.state.runtime.list_tasks()
            assert len(tasks) == 1
            owner_task = tasks[0]
            assert owner_task.delivery_recipient == "owner"
            assert owner_task.delivery_account == "bot-1"
            assert owner_task.status == "completed"

            # Ordinary conversation still works for a trusted sender, but it is
            # a separate conversation and never becomes a development Task.
            chat = WechatConversationAdapter(api)
            trusted_chat = await chat.reply(
                "hello as trusted",
                message_id="trusted-chat",
                sender="trusted",
                account="bot-1",
            )
            assert trusted_chat and "未执行" not in trusted_chat
            owner_chat = await chat.reply(
                "hello as owner",
                message_id="owner-chat",
                sender="owner",
                account="bot-1",
            )
            assert owner_chat and "未执行" not in owner_chat
            assert app.state.runtime.list_tasks() == [owner_task]
            conversations = app.state.conversations
            trusted_conversation = conversations.get_wechat_conversation(
                "bot-1", "trusted"
            )
            owner_conversation = conversations.get_wechat_conversation("bot-1", "owner")
            assert trusted_conversation is not None
            assert owner_conversation is not None
            assert (
                trusted_conversation.conversation_id
                != owner_conversation.conversation_id
            )
            trusted_history = conversations.history(
                trusted_conversation.conversation_id
            )
            assert not any(
                "hello as owner" in str(item.get("content", ""))
                for item in trusted_history
            )

            # A task created before this boundary remains operable only through
            # its own stored sender/account binding.
            old = app.state.runtime.record(
                RuntimeTask(
                    prompt="old trusted task",
                    source="wechat",
                    mode="development",
                    delivery_recipient="trusted",
                    delivery_account="bot-1",
                    status="completed",
                    result="old private result",
                ),
                "seeded",
            )
            status = await legacy.reply(
                f"/status {old.task_id}", sender="trusted", account="bot-1"
            )
            assert "old private result" in status
            cancelled = await legacy.reply(
                f"/cancel {old.task_id}", sender="trusted", account="bot-1"
            )
            assert "未找到对应的任务" not in cancelled

    asyncio.run(scenario())


def test_wechat_task_permission_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _prepare(tmp_path)
        async with app.router.lifespan_context(app):
            controls = app.state.controls
            controls._save(trusted_senders=("trusted",))

        reopened = _build(tmp_path)
        async with reopened.router.lifespan_context(reopened):
            api = _client(reopened)
            with pytest.raises(httpx.HTTPStatusError) as trusted:
                await api.submit(
                    "create after restart",
                    "wechat",
                    request_id="trusted-restart",
                    delivery_recipient="trusted",
                    delivery_account="bot-1",
                )
            assert trusted.value.response.status_code == 403
            assert reopened.state.runtime.list_tasks() == []

            owner = await api.submit(
                "owner chat after restart",
                "wechat",
                request_id="owner-restart",
                mode="conversation",
                delivery_recipient="owner",
                delivery_account="bot-1",
            )
            await asyncio.wait_for(reopened.state.runtime.queue.join(), 30)
            current = await api.inspect(owner["task_id"])
            assert current["status"] == "completed"

    asyncio.run(scenario())


def test_standalone_wechat_scope_follows_the_bound_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_home = tmp_path / "agent"
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(agent_home))
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(tmp_path / "board"))
    PersonalMemoryStore(agent_home).write_memory_md(MEMORY_MARKER)
    captured: list[dict[str, Any]] = []
    purposes: list[str] = []

    async def fake_run(**kwargs: Any) -> HeadlessResult:
        captured.append(kwargs)
        return HeadlessResult(
            task_id=str(kwargs.get("task_id") or "task"),
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
            final_response="ok",
        )

    def factory(_sender: str, _active: list[object], *, purpose: str = "task") -> Any:
        purposes.append(purpose)

        def build(_agent: object) -> object:
            return object()

        return build

    monkeypatch.setattr(serve_module, "_run_via_config_async", fake_run)
    config = ServeConfig(
        workspace=workspace,
        state_dir=tmp_path / "state",
        account=_account(),
        approval=None,
        allowed_senders=frozenset({"owner", "trusted"}),
    )

    async def scenario() -> None:
        chat = default_conversation_executor(config, factory)
        assert await chat("hello", "owner", "snapshot") == "ok"
        assert await chat("hello", "trusted", "snapshot") == "ok"
        owner_call, trusted_call = captured
        assert purposes == ["chat", "chat-guest"]

        assert owner_call["read_only"] is False
        assert MEMORY_MARKER in owner_call["personal_context"]
        owner_tools = {tool.name for tool in owner_call["extra_tools"]}
        assert {"memory_search", "schedule_manage", "task_create"} <= owner_tools

        assert trusted_call["read_only"] is True
        assert trusted_call["personal_context"] == ""
        assert tuple(trusted_call["extra_tools"]) == ()
        assert trusted_call["grant_shell"] is False
        assert trusted_call["sandbox_shell"] is False

        task = default_task_executor(config)
        denied = await task(
            "start development", asyncio.Event(), "trusted-task", sender="trusted"
        )
        assert denied.status is HeadlessStatus.UNAUTHORIZED
        assert "主人" in (denied.final_response or "")
        assert len(captured) == 2

        allowed = await task(
            "start development", asyncio.Event(), "owner-task", sender="owner"
        )
        assert allowed.status is HeadlessStatus.COMPLETED
        assert len(captured) == 3

    asyncio.run(scenario())
