"""Real Server HTTP tests for the new WeChat Conversation/Run path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from cairn.infra.runtime_client import RuntimeClient
from cairn.terminal.wechat.runtime import WechatConversationAdapter
from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess


async def _reply(
    adapter: WechatConversationAdapter,
    prompt: str,
    *,
    sender: str = "owner",
    account: str = "bot-1",
    message_id: str,
) -> str:
    return await adapter.reply(
        prompt,
        message_id=message_id,
        sender=sender,
        account=account,
        conversation_key="wechat:" + account + ":" + sender,
    )


def test_wechat_conversation_three_runs_isolation_and_restart(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        api = RuntimeClient(server.url, token)
        adapter = WechatConversationAdapter(api)
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http:
            first = asyncio.run(_reply(adapter, "first", message_id="wechat-msg-1"))
            second = asyncio.run(_reply(adapter, "second", message_id="wechat-msg-2"))
            third = asyncio.run(_reply(adapter, "third", message_id="wechat-msg-3"))
            assert first and second and third
            conversations = http.get("/api/runtime/conversations").json()
            assert len(conversations) == 1
            conversation_id = conversations[0]["conversation_id"]
            assert conversations[0]["source"] == "wechat"
            detail = http.get(f"/api/runtime/conversations/{conversation_id}").json()
            assert len(detail["runs"]) == 3
            assert http.get("/api/runtime/tasks").json() == []
            history = http.get(
                f"/api/runtime/conversations/{conversation_id}/history"
            ).json()
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "first",
                "second",
                "third",
            ]
            # Different sender and different account never share the binding.
            asyncio.run(
                _reply(
                    adapter,
                    "other sender",
                    sender="other",
                    message_id="wechat-msg-4",
                )
            )
            asyncio.run(
                _reply(
                    adapter,
                    "other account",
                    account="bot-2",
                    message_id="wechat-msg-5",
                )
            )
            assert len(http.get("/api/runtime/conversations").json()) == 3
            # Web/CLI can read and continue the same Conversation.
            web_run = asyncio.run(
                api.send_conversation(
                    conversation_id,
                    "from web",
                    source="web",
                    request_id="wechat-web-1",
                )
            )
            for _ in range(100):
                current = asyncio.run(api.get_run(web_run["run_id"]))
                if current["status"] not in {"queued", "running"}:
                    break
                asyncio.run(asyncio.sleep(0.02))
            assert current["status"] == "completed"
            cli_run = asyncio.run(
                api.send_conversation(
                    conversation_id,
                    "from cli",
                    source="cli",
                    request_id="wechat-cli-1",
                )
            )
            assert web_run["conversation_id"] == conversation_id
            assert cli_run["conversation_id"] == conversation_id
            assert (
                len(
                    http.get(f"/api/runtime/conversations/{conversation_id}").json()[
                        "runs"
                    ]
                )
                == 5
            )
        server.stop()
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        api = RuntimeClient(server.url, token)
        adapter = WechatConversationAdapter(api)
        asyncio.run(_reply(adapter, "after restart", message_id="wechat-msg-6"))
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http:
            assert len(http.get("/api/runtime/conversations").json()) == 3
            detail = http.get(f"/api/runtime/conversations/{conversation_id}").json()
            assert len(detail["runs"]) == 6
            assert http.get("/api/runtime/tasks").json() == []
    finally:
        server.stop()


def test_wechat_duplicate_message_id_creates_one_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        api = RuntimeClient(server.url, token)
        adapter = WechatConversationAdapter(api)
        first = asyncio.run(_reply(adapter, "once", message_id="same-wechat-id"))
        second = asyncio.run(_reply(adapter, "once", message_id="same-wechat-id"))
        changed = asyncio.run(_reply(adapter, "changed", message_id="same-wechat-id"))
        assert first == second
        assert "不一致" in changed or "未执行" in changed
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http:
            conversations = http.get("/api/runtime/conversations").json()
            assert len(conversations) == 1
            detail = http.get(
                f"/api/runtime/conversations/{conversations[0]['conversation_id']}"
            ).json()
            assert len(detail["runs"]) == 1
    finally:
        server.stop()
