"""WeChat status commands stay scoped to the sender's Conversation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.core.budget import RunBudget
from cairn.infra.runtime_catalog import RuntimeTask
from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.wechat.state import WechatAccount, WechatState
from cairn.runtime.task_service import TaskService
from cairn.server import runtime_app
from cairn.terminal.wechat.runtime import (
    WechatConversationAdapter,
    WechatRuntimeAdapter,
)
from tests.support.git import initialize_repository


class FakeClient:
    def __init__(
        self,
        *,
        conversation_id: str = "conv-owner-a",
        run_conversation_id: str = "conv-owner-a",
    ) -> None:
        self.conversation_id = conversation_id
        self.run_conversation_id = run_conversation_id

    async def get_wechat_conversation(
        self, _account: str, _sender: str
    ) -> dict[str, Any]:
        return {"conversation_id": self.conversation_id}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "conversation_id": self.run_conversation_id,
            "status": "completed",
            "result": "private result",
        }


def _reply(adapter: WechatConversationAdapter, text: str) -> str:
    return asyncio.run(
        adapter.reply(
            text,
            message_id="status-1",
            sender="owner-a",
            account="bot-1",
        )
    )


def test_status_rejects_run_from_another_sender() -> None:
    adapter = WechatConversationAdapter(
        FakeClient(run_conversation_id="conv-owner-b")  # type: ignore[arg-type]
    )
    answer = _reply(adapter, "/status run-other")
    assert "private result" not in answer
    assert "未找到" in answer


def test_status_rejects_cli_or_web_run() -> None:
    adapter = WechatConversationAdapter(
        FakeClient(run_conversation_id="conv-cli")  # type: ignore[arg-type]
    )
    answer = _reply(adapter, "/status run-cli")
    assert "private result" not in answer
    assert "未找到" in answer


def test_legacy_task_commands_check_ownership_in_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        state = tmp_path / "state"
        WechatState(state / "wechat").save_account(
            WechatAccount("test-bot-token", "bot-1", "https://example.test", "owner-a")
        )
        app = runtime_app(
            source,
            state,
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="runtime-token",
        )
        service: TaskService = app.state.runtime
        app.state.controls._save(trusted_senders=("owner-b",))
        client = RuntimeClient(
            "http://127.0.0.1:8777",
            "runtime-token",
            transport=httpx.ASGITransport(app=app),
        )
        legacy = WechatRuntimeAdapter(client)
        adapter = WechatConversationAdapter(client, legacy=legacy)

        async def reply(
            text: str, *, sender: str | None = "owner-a", account: str | None = "bot-1"
        ) -> str:
            # The receiver routes /task to the legacy adapter directly.
            target = legacy if text.startswith("/task ") else adapter
            return await target.reply(text, sender=sender, account=account)

        def seed(**overrides: Any) -> RuntimeTask:
            values = {
                "prompt": "private prompt",
                "source": "wechat",
                "delivery_recipient": "owner-a",
                "delivery_account": "bot-1",
                "status": "completed",
                "result": "private task result",
                **overrides,
            }
            return service.record(RuntimeTask.model_validate(values), "seeded")

        reads: list[str] = []

        def history(task: RuntimeTask) -> list[dict[str, Any]]:
            reads.append(task.task_id)
            return [{"role": "assistant", "content": "private history"}]

        def recovery(task: RuntimeTask) -> dict[str, Any]:
            reads.append(task.task_id)
            return {"detail": "private recovery"}

        service.history_reader = history
        service.recovery_reader = recovery
        async with app.router.lifespan_context(app):
            others = [
                seed(delivery_recipient="owner-b"),
                seed(delivery_recipient="owner-b", status="paused"),
                seed(delivery_recipient="owner-b", status="recovery_required"),
                seed(delivery_recipient="owner-b", status="queued"),
                seed(delivery_account="other-bot"),
                seed(delivery_account=None),
                seed(delivery_recipient=None, delivery_account=None),
                seed(source="cli", delivery_recipient=None, delivery_account=None),
                seed(source="web"),
            ]
            own = seed()
            async with httpx.AsyncClient(
                base_url=client.url, transport=client.transport
            ) as browser:
                assert (
                    await browser.post(
                        "/api/login",
                        json={"username": "cairn", "auth_passphrase": "runtime-token"},
                    )
                ).status_code == 200
                response = await browser.post(
                    "/api/runtime/commands",
                    headers={"Origin": client.url},
                    json={
                        "command": "/history",
                        "task_id": own.task_id,
                        "source": "wechat",
                        "sender": "owner-a",
                        "account": "bot-1",
                        "request_id": "browser-impersonation",
                    },
                )
                assert response.status_code == 403
            missing = "task-missing"
            commands = (
                "/status",
                "/task",
                "/history",
                "/recovery",
                "/reply",
                "/continue",
                "/cancel",
            )
            for command in commands:
                suffix = " next input" if command == "/reply" else ""
                expected = await reply(f"{command} {missing}{suffix}")
                assert expected == "未找到对应的任务。"
                for task in others:
                    assert await reply(f"{command} {task.task_id}{suffix}") == expected
                    assert service.inspect(task.task_id) == task
                assert (
                    await reply(f"{command} {own.task_id}{suffix}", account="other-bot")
                    == expected
                )
                assert (
                    await reply(f"{command} {own.task_id}{suffix}", sender=None)
                    == expected
                )
                assert (
                    await reply(f"{command} {own.task_id}{suffix}", account=None)
                    == expected
                )
                assert service.inspect(own.task_id) == own
            assert reads == []

            assert "private task result" in await reply(f"/status {own.task_id}")
            assert "private task result" in await reply(f"/task {own.task_id}")
            assert "private history" in await reply(f"/history {own.task_id}")
            interrupted = seed(status="recovery_required")
            assert "private recovery" in await reply(f"/recovery {interrupted.task_id}")
            paused = seed(status="paused")
            assert "queued" in await reply(f"/continue {paused.task_id}")
            queued = seed(status="queued")
            assert "cancelled" in await reply(f"/cancel {queued.task_id}")
            assert "queued" in await reply(f"/reply {own.task_id} next input")

            async def cancel_with_concurrent_reassignment(task_id: str) -> RuntimeTask:
                return service.record(
                    service.inspect(task_id).model_copy(
                        update={
                            "delivery_recipient": "owner-b",
                            "result": "new private result",
                        }
                    ),
                    "reassigned",
                )

            with monkeypatch.context() as patch:
                patch.setattr(service, "cancel", cancel_with_concurrent_reassignment)
                reassigned = seed()
                assert (
                    await reply(f"/cancel {reassigned.task_id}") == "未找到对应的任务。"
                )

            # Host access remains available across WeChat senders and sources.
            assert (await client.inspect(others[0].task_id))[
                "result"
            ] == "private task result"
            assert (await client.control(others[3].task_id, "cancel"))[
                "status"
            ] == "cancelled"
            web_task = others[-1]
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await client.message(
                    web_task.task_id,
                    "take over",
                    source="wechat",
                    request_id="direct-wechat",
                    expected_sequence=web_task.sequence,
                    delivery_recipient="owner-a",
                    delivery_account="bot-1",
                )
            assert denied.value.response.status_code == 404
            assert service.inspect(web_task.task_id) == web_task
            assert (
                await client.message(
                    web_task.task_id,
                    "host input",
                    source="web",
                    request_id="host",
                    expected_sequence=web_task.sequence,
                )
            )["status"] == "queued"

    asyncio.run(scenario())
