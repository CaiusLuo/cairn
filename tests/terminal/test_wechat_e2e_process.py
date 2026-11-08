"""Real ServerProcess + server-owned WeChat receiver + FakeILink acceptance."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.wechat.state import WechatState
from cairn.server_controls import ControlState
from tests.support.fake_ilink import FakeILink
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess
from tests.support.wechat_service import _account, _msg


async def _wait_until(predicate: Any, timeout: float = 30) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_real_server_process_three_wechat_runs_one_final_each(
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
                wechat_enabled=True, trusted_senders=("owner",)
            ).model_dump_json()
        )
        server.start()
        try:
            token = (server.state / "runtime.token").read_text().strip()
            client = RuntimeClient(server.url, token)
            for index, prompt in enumerate(("first", "second", "third"), 1):
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg(prompt, sender="owner", mid=100 + index)],
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
                while not fake.sent:
                    await asyncio.sleep(0.02)
            assert len(fake.sent) == 1
            completed = state.get_delivery_intent(key)
            assert completed is not None
            assert completed["delivery_status"] == "sent"
        finally:
            server.stop()

    with FakeILink() as fake:
        asyncio.run(scenario(fake))
