"""Telegram adapter protocol tests against an injected aiogram session."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, GetUpdates, SendMessage
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import Chat, Message, Update, User
from aiohttp import web

from cairn.channels.adapter import ChannelAdapterRegistry
from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelConnection,
    ChannelCredentialError,
    ChannelDesiredState,
    ChannelInboundEvent,
    ChannelInboundRejected,
    ChannelInboundUncertain,
    ChannelPlatform,
    ChannelSendStatus,
)
from cairn.channels.store import ChannelStore
from cairn.channels.telegram import (
    TelegramAdapter,
    split_message,
    telegram_adapter_factory,
)
from tests.channels.support import FakeIntake

TOKEN = "123456:" + "a" * 35
OTHER_TOKEN = "654321:" + "b" * 35
BOT_ID = 999


class FakeSession(BaseSession):
    """In-process Telegram Bot API: no sockets, same aiogram call surface."""

    def __init__(self) -> None:
        super().__init__()
        self.me = User(id=BOT_ID, is_bot=True, first_name="Cairn")
        self.updates: asyncio.Queue[list[Update]] = asyncio.Queue()
        self.offsets: list[int | None] = []
        self.sent: list[tuple[int, str, object]] = []
        self.send_attempts = 0
        self.me_error: BaseException | None = None
        self.get_updates_error: BaseException | None = None
        self.send_script: list[BaseException | None] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        if isinstance(method, GetMe):
            if self.me_error is not None:
                raise self.me_error
            return self.me  # type: ignore[return-value]
        if isinstance(method, GetUpdates):
            self.offsets.append(method.offset)
            if self.get_updates_error is not None:
                raise self.get_updates_error
            updates = await self.updates.get()
            return updates  # type: ignore[return-value]
        if isinstance(method, SendMessage):
            self.send_attempts += 1
            action = self.send_script.pop(0) if self.send_script else None
            if action is not None:
                raise action
            chat_id = int(method.chat_id)  # type: ignore[arg-type]
            parse_mode = (
                method.parse_mode if isinstance(method.parse_mode, str) else None
            )
            self.sent.append((chat_id, method.text, parse_mode))
            return Message(  # type: ignore[return-value]
                message_id=len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram method: {method}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""


class RecordingStore:
    def __init__(self, store: ChannelStore, events: list[str]) -> None:
        self.store = store
        self.events = events

    def load_adapter_state(self, connection_id: str, key: str) -> dict[str, Any] | None:
        return self.store.load_adapter_state(connection_id, key)

    def save_adapter_state(
        self, connection_id: str, key: str, value: dict[str, Any]
    ) -> None:
        self.events.append("offset")
        self.store.save_adapter_state(connection_id, key, value)


def connection(connection_id: str = "ch-telegram") -> ChannelConnection:
    return ChannelConnection(
        connection_id=connection_id,
        platform=ChannelPlatform.TELEGRAM,
        display_name="Telegram",
        enabled=True,
        desired_state=ChannelDesiredState.RUNNING,
        owner_identity="7",
    )


def update(
    update_id: int,
    *,
    text: str = "hello",
    chat_id: int = 7,
    user_id: int = 7,
    chat_type: str = "private",
    is_bot: bool = False,
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=is_bot, first_name="Sender"),
            text=text,
        ),
    )


async def no_sleep(_: float) -> None:
    return None


async def yield_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


async def ignore(event: ChannelInboundEvent) -> None:
    return None


def adapter_with(
    session: FakeSession,
    store: Any,
    sink: Any,
    *,
    connection_id: str = "ch-telegram",
    credentials: dict[str, str] | None = None,
    sleep: Any = no_sleep,
) -> TelegramAdapter:
    return TelegramAdapter(
        connection(connection_id),
        credentials if credentials is not None else {"bot_token": TOKEN},
        sink,
        store=store,
        bot_factory=lambda token: Bot(token=token, session=session),
        sleep=sleep,
    )


def test_split_message_respects_utf16_limit_and_prefers_newlines() -> None:
    text = ("a" * 4100) + "\n" + ("b" * 4100)
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)
    assert chunks[0].endswith("a")
    assert chunks[1].startswith("a")

    emoji = "😀" * 3000  # 6000 UTF-16 code units
    chunks = split_message(emoji)
    assert len(chunks) == 2
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)


def test_invalid_and_missing_tokens_never_touch_the_network(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        for credentials, expected in (
            (None, "telegram_token_missing"),
            ({"bot_token": "not-a-token"}, "telegram_token_invalid"),
        ):
            adapter = TelegramAdapter(
                connection(),
                credentials,
                ignore,
                store=store,
                bot_factory=lambda token: pytest.fail("network must not be reached"),
            )
            with pytest.raises(ChannelCredentialError) as excinfo:
                await adapter.start()
            assert str(excinfo.value) == expected
            assert adapter.health().actual_state is ChannelActualState.ERROR

    asyncio.run(scenario())


def test_unauthorized_token_is_reported_without_echoing_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        session.me_error = TelegramUnauthorizedError(GetMe(), "Unauthorized")
        adapter = adapter_with(session, store, ignore)
        with pytest.raises(ChannelCredentialError) as excinfo:
            await adapter.start()
        assert str(excinfo.value) == "telegram_invalid_token"
        assert TOKEN not in (adapter.health().last_error or "")
        assert session.closed is True

    asyncio.run(scenario())


def test_duplicate_token_cannot_open_a_second_poller(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        first_session = FakeSession()
        second_session = FakeSession()
        first = adapter_with(first_session, store, ignore)
        second = adapter_with(second_session, store, ignore, connection_id="ch-second")
        await first.start()
        with pytest.raises(ChannelCredentialError) as excinfo:
            await second.start()
        assert str(excinfo.value) == "telegram_token_in_use"
        assert second.health().actual_state is ChannelActualState.ERROR
        await first.stop()
        await second.start()
        assert second.health().actual_state is ChannelActualState.RUNNING
        await second.stop()

    asyncio.run(scenario())


def test_private_update_is_persisted_before_the_offset_advances(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        events: list[str] = []
        seen: list[ChannelInboundEvent] = []

        async def sink(event: ChannelInboundEvent) -> None:
            events.append("inbound")
            seen.append(event)

        session = FakeSession()
        adapter = adapter_with(session, RecordingStore(store, events), sink)
        await adapter.start()
        await session.updates.put([update(41, text="hi there")])
        await wait_until(lambda: bool(seen))
        await wait_until(
            lambda: store.load_adapter_state("ch-telegram", "telegram") is not None
        )
        assert events == ["inbound", "offset"]
        assert seen[0].platform is ChannelPlatform.TELEGRAM
        assert seen[0].external_message_id == "7:41"
        assert seen[0].sender_id == "7"
        assert seen[0].conversation_binding == "7"
        assert seen[0].text == "hi there"
        assert store.load_adapter_state("ch-telegram", "telegram") == {"offset": 42}
        await adapter.stop()

    asyncio.run(scenario())


def test_group_bot_and_self_messages_are_acknowledged_without_intake(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        seen: list[ChannelInboundEvent] = []

        async def sink(event: ChannelInboundEvent) -> None:
            seen.append(event)

        session = FakeSession()
        adapter = adapter_with(session, store, sink)
        await adapter.start()
        await session.updates.put(
            [
                update(10, chat_type="group"),
                update(11, is_bot=True),
                update(12, user_id=BOT_ID),
                update(13, text="   "),
            ]
        )
        await wait_until(
            lambda: (
                store.load_adapter_state("ch-telegram", "telegram") == {"offset": 14}
            )
        )
        assert seen == []
        await adapter.stop()

    asyncio.run(scenario())


def test_definite_rejection_advances_and_uncertain_redelivery_reuses_the_update(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        calls = 0

        async def rejecting(event: ChannelInboundEvent) -> None:
            raise ChannelInboundRejected("sender is not authorized")

        adapter = adapter_with(FakeSession(), store, rejecting)
        await adapter.start()
        assert adapter._bot is not None
        session = adapter._bot.session
        assert isinstance(session, FakeSession)
        await session.updates.put([update(20)])
        await wait_until(
            lambda: (
                store.load_adapter_state("ch-telegram", "telegram") == {"offset": 21}
            )
        )
        await adapter.stop()

        async def uncertain_then_ok(event: ChannelInboundEvent) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ChannelInboundUncertain("admission interrupted")

        store = ChannelStore(tmp_path / "second")
        adapter = adapter_with(FakeSession(), store, uncertain_then_ok)
        await adapter.start()
        assert adapter._bot is not None
        session = adapter._bot.session
        assert isinstance(session, FakeSession)
        duplicated = update(30)
        await session.updates.put([duplicated])
        await session.updates.put([duplicated])
        await wait_until(
            lambda: (
                store.load_adapter_state("ch-telegram", "telegram") == {"offset": 31}
            )
        )
        assert calls == 2
        assert session.offsets[:2] == [None, None]
        await adapter.stop()

    asyncio.run(scenario())


def test_restart_resumes_from_the_persisted_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        store.save_adapter_state("ch-telegram", "telegram", {"offset": 77})
        session = FakeSession()
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        await wait_until(lambda: bool(session.offsets))
        assert session.offsets[0] == 77
        await adapter.stop()

    asyncio.run(scenario())


def test_send_splits_long_text_and_falls_back_from_markdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        body = ("a" * 4100) + "\n" + ("b" * 4100)
        result = await adapter.send("42", body, "key-1")
        assert result.status is ChannelSendStatus.ACCEPTED
        assert len(session.sent) == 3
        assert [chunk[2] for chunk in session.sent] == ["Markdown"] * 3
        assert "".join(chunk[1] for chunk in session.sent) == body
        await adapter.stop()

    asyncio.run(scenario())


def test_markdown_parse_error_retries_as_plain_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        session.send_script = [
            TelegramBadRequest(
                SendMessage(chat_id=42, text="x"), "can't parse entities"
            )
        ]
        result = await adapter.send("42", "*unclosed", "key-2")
        assert result.status is ChannelSendStatus.ACCEPTED
        assert session.sent == [(42, "*unclosed", None)]
        await adapter.stop()

    asyncio.run(scenario())


def test_rate_limit_is_retried_after_the_advertised_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        session.send_script = [
            TelegramRetryAfter(
                SendMessage(chat_id=42, text="x"), "Too Many Requests", 0
            )
        ]
        result = await adapter.send("42", "hello", "key-3")
        assert result.status is ChannelSendStatus.ACCEPTED
        assert session.send_attempts == 2
        assert len(session.sent) == 1
        await adapter.stop()

    asyncio.run(scenario())


def test_partial_send_is_never_reported_as_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        session.send_script = [
            None,
            TelegramForbiddenError(SendMessage(chat_id=42, text="x"), "blocked"),
        ]
        result = await adapter.send("42", ("a" * 4100) + "\n" + ("b" * 4100), "key-4")
        assert result.status is ChannelSendStatus.UNKNOWN
        assert "partial" in (result.error or "")
        await adapter.stop()

    asyncio.run(scenario())


def test_send_requires_a_running_adapter(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        adapter = adapter_with(FakeSession(), store, ignore)
        with pytest.raises(ChannelAdapterError):
            await adapter.send("42", "hello", "key-5")

    asyncio.run(scenario())


def test_poll_loop_turns_a_token_conflict_into_observable_error(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        session.get_updates_error = TelegramConflictError(GetUpdates(), "Conflict")
        adapter = adapter_with(session, store, ignore)
        await adapter.start()
        await wait_until(
            lambda: adapter.health().actual_state is ChannelActualState.ERROR
        )
        assert "telegram_conflict" in (adapter.health().last_error or "")
        await adapter.stop()

    asyncio.run(scenario())


def test_network_loss_reconnects_without_stopping_the_adapter(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        session = FakeSession()
        session.get_updates_error = TelegramNetworkError(GetUpdates(), "disconnected")
        adapter = adapter_with(session, store, ignore, sleep=yield_sleep)
        await adapter.start()
        await wait_until(
            lambda: adapter.health().actual_state is ChannelActualState.CONNECTING
        )
        session.get_updates_error = None
        await session.updates.put([update(50)])
        await wait_until(
            lambda: (
                store.load_adapter_state("ch-telegram", "telegram") == {"offset": 51}
            )
        )
        assert adapter.health().actual_state is ChannelActualState.RUNNING
        await adapter.stop()

    asyncio.run(scenario())


def test_aiogram_http_boundary_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = {"sent_update": False}
        gate = asyncio.Event()
        calls: list[tuple[str, str]] = []

        async def handler(request: web.Request) -> web.Response:
            method = request.match_info["method"]
            calls.append((request.match_info["token"], method))
            if method == "getMe":
                return web.json_response(
                    {
                        "ok": True,
                        "result": {
                            "id": BOT_ID,
                            "is_bot": True,
                            "first_name": "Cairn",
                            "username": "cairn_bot",
                        },
                    }
                )
            if method == "getUpdates":
                if not state["sent_update"]:
                    state["sent_update"] = True
                    return web.json_response(
                        {
                            "ok": True,
                            "result": [
                                {
                                    "update_id": 70,
                                    "message": {
                                        "message_id": 70,
                                        "date": 1,
                                        "chat": {"id": 7, "type": "private"},
                                        "from": {
                                            "id": 7,
                                            "is_bot": False,
                                            "first_name": "Owner",
                                        },
                                        "text": "hello over http",
                                    },
                                }
                            ],
                        }
                    )
                await gate.wait()
                return web.json_response({"ok": True, "result": []})
            if method == "sendMessage":
                body = await request.post()
                return web.json_response(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 1,
                            "date": 1,
                            "chat": {"id": body["chat_id"], "type": "private"},
                            "text": body["text"],
                        },
                    }
                )
            return web.json_response(
                {"ok": False, "error_code": 404, "description": "Not Found"},
                status=404,
            )

        app = web.Application()
        app.router.add_post("/bot{token}/{method}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        session: AiohttpSession | None = None
        try:
            assert site._server is not None
            server = cast(Any, site._server)
            port = server.sockets[0].getsockname()[1]
            api = TelegramAPIServer.from_base(f"http://127.0.0.1:{port}")
            session = AiohttpSession(api=api)
            store = ChannelStore(tmp_path / "channels")
            seen: list[ChannelInboundEvent] = []

            async def sink(event: ChannelInboundEvent) -> None:
                seen.append(event)

            adapter = TelegramAdapter(
                connection(),
                {"bot_token": TOKEN},
                sink,
                store=store,
                bot_factory=lambda token: Bot(token=token, session=session),
                sleep=yield_sleep,
            )
            await adapter.start()
            await wait_until(
                lambda: (
                    store.load_adapter_state("ch-telegram", "telegram")
                    == {"offset": 71}
                )
            )
            assert seen[0].text == "hello over http"
            result = await adapter.send("7", "reply over http", "key-http")
            assert result.status is ChannelSendStatus.ACCEPTED
            assert [method for _, method in calls] == [
                "getMe",
                "getUpdates",
                "getUpdates",
                "sendMessage",
            ]
            assert all(token == TOKEN for token, _ in calls)
            await adapter.stop()
        finally:
            gate.set()
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_manager_admits_an_owner_once_and_rejects_another_sender(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(tmp_path / "channels")
        intake = FakeIntake()
        session = FakeSession()
        registry = ChannelAdapterRegistry().register(
            ChannelPlatform.TELEGRAM,
            telegram_adapter_factory(
                store,
                bot_factory=lambda token: Bot(token=token, session=session),
                sleep=no_sleep,
            ),
        )
        manager = ChannelManager(
            store=store,
            conversations=intake,
            adapters=registry,
            retry_delays=(0,),
        )
        created = await manager.create_connection(
            platform=ChannelPlatform.TELEGRAM,
            display_name="Telegram",
            enabled=True,
            owner_identity="7",
            metadata={},
            credentials={"bot_token": TOKEN},
            request_id="create-telegram",
            allowed_senders=("8",),
        )
        connection_id = created.connection.connection_id
        await manager.start_connection(connection_id, "start-telegram")

        duplicate = update(100, text="介绍一下 Cairn", user_id=7, chat_id=7)
        await session.updates.put([duplicate, duplicate])
        await session.updates.put([update(101, text="and again", user_id=8, chat_id=8)])
        await wait_until(
            lambda: (
                store.load_adapter_state(connection_id, "telegram") == {"offset": 102}
            )
        )
        assert len(intake.submissions) == 2
        owner_metadata = intake.metadata[0]
        assert owner_metadata is not None
        owner_value = owner_metadata["channel_principal"]
        assert isinstance(owner_value, dict)
        owner = cast(dict[str, object], owner_value)
        assert owner["role"] == "owner"
        assert owner["sender_id"] == "7"
        assert owner["connection_id"] == connection_id
        guest_metadata = intake.metadata[1]
        assert guest_metadata is not None
        guest_value = guest_metadata["channel_principal"]
        assert isinstance(guest_value, dict)
        guest = cast(dict[str, object], guest_value)
        assert guest["role"] == "allowed"
        assert guest["sender_id"] == "8"

        await session.updates.put([update(102, text="hello", user_id=9, chat_id=9)])
        await wait_until(
            lambda: (
                store.load_adapter_state(connection_id, "telegram") == {"offset": 103}
            )
        )
        assert len(intake.submissions) == 2
        await manager.stop_connection(connection_id, "stop-telegram")
        await manager.close()

    asyncio.run(scenario())
