"""Real ServerProcess + server-owned WeChat receiver + FakeILink acceptance."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.infra.runtime_client import RuntimeClient
from cairn.integrations.wechat.state import WechatState
from cairn.server_controls import ControlState
from tests.support.fake_ilink import FakeILink
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess
from tests.support.wechat_service import _account, _msg


async def _wait_until(predicate: Any, timeout: float = 30) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_real_server_process_wechat_chat_and_development_delivery(
    tmp_path: Path,
) -> None:
    async def scenario(fake: FakeILink) -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        server = ServerProcess(source, tmp_path)
        WechatState(server.data_root / "wechat").save_account(_account(fake))
        server.state.mkdir(parents=True, exist_ok=True)
        (server.state / "controls.json").write_text(
            ControlState(
                wechat_enabled=True, trusted_senders=("owner", "other-sender")
            ).model_dump_json()
        )
        server.start()
        try:
            token = (server.state / "runtime.token").read_text().strip()
            client = RuntimeClient(server.url, token)
            state = WechatState(server.data_root / "wechat")
            for index, prompt in enumerate(("first", "second", "third"), 1):
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [
                            _msg(
                                prompt,
                                sender="owner",
                                mid=100 + index,
                                context=f"chat-{index}",
                            )
                        ]
                        * 2,
                    }
                )
                async with asyncio.timeout(30):
                    while True:
                        conversations = await client.request("GET", "conversations")
                        if conversations:
                            detail = await client.request(
                                "GET",
                                f"conversations/{conversations[0]['conversation_id']}",
                            )
                            if len(detail["runs"]) >= index:
                                break
                        await asyncio.sleep(0.02)
                await _wait_until(lambda index=index: len(fake.sent) >= index)
            conversations = await client.request("GET", "conversations")
            assert len(conversations) == 1
            detail = await client.request(
                "GET", f"conversations/{conversations[0]['conversation_id']}"
            )
            assert len(detail["runs"]) == 3
            assert all(run["status"] == "completed" for run in detail["runs"])
            assert len(fake.sent) == 3
            assert await client.request("GET", "tasks") == []

            assert [message["context_token"] for message in fake.sent] == [
                "chat-1",
                "chat-2",
                "chat-3",
            ]

            development = _msg(
                "/task Create an acceptance artifact",
                sender="owner",
                mid=110,
                context="development",
            )
            fake.updates.append({"ret": 0, "msgs": [development, development]})
            await _wait_until(lambda: len(fake.sent) >= 5)
            await _wait_until(
                lambda: (
                    (WechatState(state.root).get_record("msg:110") or {}).get("state")
                    == "command"
                )
            )
            tasks = await client.request("GET", "tasks")
            assert len(tasks) == 1
            task = tasks[0]
            assert task["status"] == "completed"
            assert task["source"] == "wechat"
            assert task["delivery_recipient"] == "owner"
            assert task["delivery_account"] == "bot-1"
            development_messages = fake.sent[3:]
            assert len(development_messages) == 2
            assert all(
                msg["context_token"] == "development" for msg in development_messages
            )
            texts = [
                msg["item_list"][0]["text_item"]["text"] for msg in development_messages
            ]
            assert sum("开发任务已开始" in text for text in texts) == 1
            assert sum(text.startswith("开发任务已完成") for text in texts) == 1

            fake.updates.append(
                {
                    "ret": 0,
                    "msgs": [
                        _msg(
                            f"/task {task['task_id']}",
                            sender="other-sender",
                            mid=111,
                            context="denied",
                        ),
                        _msg(
                            f"/history {task['task_id']}",
                            sender="owner",
                            mid=112,
                            context="allowed",
                        ),
                    ],
                }
            )
            await _wait_until(lambda: len(fake.sent) >= 7)
            answers = {
                message["context_token"]: message["item_list"][0]["text_item"]["text"]
                for message in fake.sent[5:]
            }
            assert answers["denied"] == "未找到对应的任务。"
            assert "已提交历史" in answers["allowed"]
            assert len(fake.sent) == 7
            assert len(await client.request("GET", "tasks")) == 1
            detail = await client.request(
                "GET", f"conversations/{conversations[0]['conversation_id']}"
            )
            assert len(detail["runs"]) == 3
        finally:
            server.stop()

    with FakeILink() as fake:
        asyncio.run(scenario(fake))


def test_pending_delivery_survives_server_restart(tmp_path: Path) -> None:
    async def scenario(fake: FakeILink) -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        server = ServerProcess(source, tmp_path)
        server.start()
        try:
            token = (server.state / "runtime.token").read_text().strip()
            client = RuntimeClient(server.url, token)
            conversation = await client.ensure_wechat_conversation(
                "bot-1", "owner", "restart prompt"
            )
            request_id = "wechat:restart-e2e"
            run = await client.send_conversation(
                conversation["conversation_id"],
                "restart prompt",
                source="wechat",
                request_id=request_id,
            )
            async with asyncio.timeout(30):
                while True:
                    current = await client.get_run(run["run_id"])
                    if current["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.02)
            state = WechatState(server.data_root / "wechat")
            state.record_received(
                "msg:restart",
                sender="owner",
                context_token="restart-context",
                text="restart prompt",
            )
            key = state.begin_delivery_intent(
                message_key="msg:restart",
                message_id="restart-message",
                request_id=request_id,
                account_id="bot-1",
                sender_id="owner",
                context_token="restart-context",
                content="restart prompt",
            )
            state.bind_delivery_intent(
                key,
                conversation_id=conversation["conversation_id"],
                run_id=run["run_id"],
            )
            # No account was configured yet, so the coordinator must leave
            # the intent pending instead of consuming it.
            pending = state.get_delivery_intent(key)
            assert pending is not None
            assert pending["delivery_status"] == "pending"
        finally:
            server.stop()

        state.save_account(_account(fake))
        server.start()
        try:
            async with asyncio.timeout(30):
                while True:
                    completed = state.get_delivery_intent(key)
                    if completed is not None and completed["delivery_status"] == "sent":
                        break
                    await asyncio.sleep(0.02)
            assert len(fake.sent) == 1
            assert completed["delivery_status"] == "sent"
        finally:
            server.stop()

    with FakeILink() as fake:
        asyncio.run(scenario(fake))


def test_corrupt_wechat_state_blocks_admission_until_restored(tmp_path: Path) -> None:
    async def scenario(fake: FakeILink) -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        server = ServerProcess(source, tmp_path)
        state = WechatState(server.data_root / "wechat")
        state.save_account(_account(fake))
        server.state.mkdir(parents=True)
        (server.state / "controls.json").write_text(
            ControlState(
                wechat_enabled=True, trusted_senders=("owner",)
            ).model_dump_json()
        )
        previous = _msg("already answered", sender="owner", mid=201)
        held = _msg("arrived during corruption", sender="owner", mid=202)
        fake.updates.append({"ret": 0, "msgs": [previous]})
        server.start()
        try:
            client = RuntimeClient(
                server.url, (server.state / "runtime.token").read_text().strip()
            )
            await _wait_until(
                lambda: (
                    len(state.list_delivery_intents(statuses=frozenset({"sent"}))) == 1
                )
            )
            await _wait_until(
                lambda: (
                    (WechatState(state.root).get_record("msg:201") or {}).get("state")
                    == "command"
                )
            )
            good = state.state_path.read_bytes()
            broken = b'{"version":'
            state.state_path.write_bytes(broken)
            fake.updates.append({"ret": 0, "msgs": [previous, held]})
            async with asyncio.timeout(30):
                while True:
                    management = await client.request(
                        "GET", "management?section=wechat"
                    )
                    if (
                        "WeChat runtime state is unreadable"
                        in management["wechat"]["status"]
                    ):
                        break
                    await asyncio.sleep(0.02)
            assert state.state_path.read_bytes() == broken
            conversations = await client.request("GET", "conversations")
            assert len(conversations) == 1
            path = f"conversations/{conversations[0]['conversation_id']}"
            original_runs = (await client.request("GET", path))["runs"]
            assert len(original_runs) == 1
            assert len(fake.sent) == 1
            assert await client.request("GET", "tasks") == []
        finally:
            server.stop()

        state.state_path.write_bytes(good)
        fake.updates.append({"ret": 0, "msgs": [previous, held]})
        server.start()
        try:
            await _wait_until(
                lambda: (
                    len(state.list_delivery_intents(statuses=frozenset({"sent"}))) == 2
                )
            )
            runs = (await client.request("GET", path))["runs"]
            assert len(runs) == 2
            assert runs[0]["run_id"] == original_runs[0]["run_id"]
            assert all(run["status"] == "completed" for run in runs)
            assert len(fake.sent) == 2
        finally:
            server.stop()

    with FakeILink() as fake:
        asyncio.run(scenario(fake))
