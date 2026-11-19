"""Offline protocol and delivery acceptance for the production Gateway adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.testclient import TestClient
from websockets.protocol import State

from cairn.channels import gateway
from cairn.channels.adapter import ChannelAdapter, ChannelAdapterRegistry, InboundSink
from cairn.channels.discord import DiscordAdapter
from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelConnection,
    ChannelDeliveryUncertain,
    ChannelPlatform,
    ChannelSendStatus,
)
from cairn.channels.qq import QQAdapter
from cairn.channels.store import ChannelStore
from tests.channels.support import FakeIntake
from tests.support.control_runtime import control_runtime
from tests.support.pg import database_for

_OWNER = "100"
_BOT = "200"
_MSG = "300"
_TARGET = "400"
_REPLY = "500"
_QQ_AUTH = "fixture-access-token"
_DISCORD_AUTH = "fixture-discord-token"


class Socket:
    def __init__(self) -> None:
        self.state = State.OPEN
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.emit({"op": 10, "d": {"heartbeat_interval": 30_000}})

    async def recv(self) -> str:
        return await self.frames.get()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self, code: int = 1000) -> None:
        self.state = State.CLOSED

    def emit(self, frame: dict[str, Any]) -> None:
        self.frames.put_nowait(json.dumps(frame))

    def dispatch(self, kind: str, sequence: int, data: dict[str, Any]) -> None:
        self.emit({"op": 0, "t": kind, "s": sequence, "d": data})


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("platform", [ChannelPlatform.QQ, ChannelPlatform.DISCORD])
def test_gateway_reconnect_resumes_and_replies_without_resending_unknown_outcomes(
    tmp_path: Path,
    platform: ChannelPlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "_RETRY_SECONDS", (0,))

    async def scenario() -> None:
        qq = platform is ChannelPlatform.QQ
        target = _OWNER if qq else _TARGET
        url = (
            "wss://api.sgroup.qq.com/websocket/"
            if qq
            else "wss://gateway.discord.gg/?v=10&encoding=json"
        )
        first, resumed, restarted = Socket(), Socket(), Socket()
        pending = [first, resumed, restarted]
        attempts: list[dict[str, Any]] = []
        uncertain = False
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        intake = FakeIntake()

        @asynccontextmanager
        async def open_socket(address: str) -> AsyncIterator[Socket]:
            assert address == url
            ws = pending.pop(0)
            try:
                yield ws
            finally:
                await ws.close()

        async def rest(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/app/getAppAccessToken":
                assert qq and request.url.host == "bots.qq.com"
                assert json.loads(request.content)["appId"] == "1234"
                return httpx2.Response(
                    200, json={"access_token": _QQ_AUTH, "expires_in": 7200}
                )
            if qq:
                assert request.headers["Authorization"] == f"QQBot {_QQ_AUTH}"
                assert request.url.host == "api.sgroup.qq.com"
                if request.url.path == "/gateway":
                    return httpx2.Response(200, json={"url": url})
                assert request.url.path == f"/v2/users/{target}/messages"
            else:
                assert request.headers["Authorization"] == f"Bot {_DISCORD_AUTH}"
                assert request.url.host == "discord.com"
                assert request.url.path == f"/api/v10/channels/{target}/messages"
            attempts.append(json.loads(request.content))
            if uncertain:
                raise httpx2.ReadTimeout("fixture disconnect", request=request)
            return httpx2.Response(
                200, json={"id": _REPLY, "channel_id": target, "author": {"id": _BOT}}
            )

        transport = httpx2.MockTransport(rest)

        def factory(
            connection: ChannelConnection,
            credentials: dict[str, str] | None,
            inbound: InboundSink,
        ) -> ChannelAdapter:
            if qq:
                return QQAdapter(
                    connection,
                    credentials,
                    inbound,
                    store=store,
                    socket=open_socket,
                    transport=transport,
                )
            return DiscordAdapter(
                connection,
                credentials,
                inbound,
                socket=open_socket,
                transport=transport,
            )

        def build_manager() -> ChannelManager:
            return ChannelManager(
                store=ChannelStore(store.db, store.root),
                conversations=intake,
                adapters=ChannelAdapterRegistry().register(platform, factory),
            )

        manager = build_manager()

        def message(
            socket: Socket, sequence: int, ident: str, *, owner: str = _OWNER
        ) -> None:
            if qq:
                socket.dispatch(
                    "C2C_MESSAGE_CREATE",
                    sequence,
                    {
                        "id": ident,
                        "author": {"user_openid": owner},
                        "content": "hello",
                    },
                )
            else:
                socket.dispatch(
                    "MESSAGE_CREATE",
                    sequence,
                    {
                        "id": ident,
                        "author": {"id": owner},
                        "content": "hello",
                        "channel_id": target,
                        "type": 0,
                    },
                )

        for socket in (first, restarted):
            socket.dispatch(
                "READY",
                1,
                {
                    "user": {"id": _BOT, "bot": True},
                    "session_id": "fixture-session",
                    "resume_gateway_url": "wss://gateway.discord.gg",
                },
            )
        try:
            created = await manager.create_connection(
                platform=platform,
                display_name=platform.value,
                enabled=True,
                owner_identity=_OWNER,
                metadata={},
                credentials={"app_id": "1234", "client_secret": "fixture-secret"}
                if qq
                else {"bot_token": _DISCORD_AUTH},
                request_id="create",
            )
            connection_id = created.connection.connection_id
            started = await manager.start_connection(connection_id, "start")
            assert started.runtime.actual_state is ChannelActualState.RUNNING
            assert first.sent[0]["op"] == 2
            assert first.sent[0]["d"]["intents"] == (1 << (25 if qq else 12))

            if qq:
                first.dispatch(
                    "GROUP_AT_MESSAGE_CREATE",
                    2,
                    {
                        "id": "305",
                        "author": {"user_openid": _OWNER},
                        "content": "group",
                    },
                )
            else:
                first.dispatch(
                    "MESSAGE_CREATE",
                    2,
                    {
                        "id": "305",
                        "author": {"id": _OWNER},
                        "content": "guild",
                        "channel_id": target,
                        "type": 0,
                        "guild_id": "900",
                    },
                )
            message(first, 3, "302", owner="999")
            message(first, 4, _MSG)
            await _until(lambda: store.reply_for_run("run-1") is not None)
            reply = store.reply_for_run("run-1")
            assert reply is not None
            assert len(intake.submissions) == 1
            assert reply.adapter_identity == _BOT and reply.target == target

            result = await manager.send_reply(
                reply, "答" * 950 if qq else "hello", "channel-reply:run-1"
            )
            assert result.status is ChannelSendStatus.ACCEPTED
            if qq:
                assert attempts == [
                    {
                        "msg_type": 0,
                        "content": attempts[0]["content"],
                        "msg_id": _MSG,
                        "msg_seq": 1,
                    }
                ]
                assert len(attempts[0]["content"].encode()) <= 2000
                assert "完整结果" in attempts[0]["content"]
            else:
                assert attempts[0]["allowed_mentions"] == {"parse": []}
                assert attempts[0]["enforce_nonce"] is True

            first.emit({"op": 7})
            await _until(lambda: bool(resumed.sent))
            assert resumed.sent[0]["op"] == 6
            assert resumed.sent[0]["d"]["seq"] == 4
            assert (
                manager.get_runtime_status(connection_id).actual_state
                is ChannelActualState.CONNECTING
            )
            message(resumed, 4, _MSG)
            message(resumed, 5, "301")
            resumed.dispatch("RESUMED", 6, {})
            await _until(lambda: store.reply_for_run("run-2") is not None)
            assert len(intake.submissions) == 2

            second_reply = store.reply_for_run("run-2")
            assert second_reply is not None
            uncertain = True
            result = await manager.send_reply(
                second_reply, "hello again", "channel-reply:run-2"
            )
            assert result.status is ChannelSendStatus.UNKNOWN
            with pytest.raises(ChannelDeliveryUncertain):
                await manager.send_reply(
                    second_reply, "hello again", "channel-reply:run-2"
                )
            assert len(attempts) == 2

            await manager.close()
            assert resumed.state is State.CLOSED
            manager = build_manager()
            await manager.start()
            await _until(
                lambda: (
                    manager.get_runtime_status(connection_id).actual_state
                    is ChannelActualState.RUNNING
                )
            )
            message(restarted, 2, _MSG)
            restarted.emit({"op": 1})
            await _until(lambda: any(frame["op"] == 1 for frame in restarted.sent))
            # The recreated manager resolves both the inbound and outbound receipts.
            assert len(intake.submissions) == 2
            with pytest.raises(ChannelDeliveryUncertain):
                await manager.send_reply(
                    second_reply, "hello again", "channel-reply:run-2"
                )
            assert len(attempts) == 2

            stopped = await manager.stop_connection(connection_id, "stop")
            assert stopped.runtime.actual_state is ChannelActualState.STOPPED
            assert restarted.state is State.CLOSED
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_production_capabilities_include_qq_and_discord(tmp_path: Path) -> None:
    app, api = control_runtime(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8777") as client:
        response = client.get(
            "/api/channels/capabilities",
            headers={"Authorization": "Bearer " + api.token},
        )
        assert response.status_code == 200
        capabilities = {row["platform"]: row for row in response.json()["platforms"]}
        for platform in ("qq", "discord"):
            assert capabilities[platform]["adapter_supported"] is True
            assert {"start", "stop", "reconnect"} <= set(
                capabilities[platform]["supported_actions"]
            )
        assert capabilities["wecom"]["adapter_supported"] is False
