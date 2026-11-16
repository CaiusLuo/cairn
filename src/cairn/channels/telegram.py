"""Telegram Bot adapter using aiogram long polling.

The adapter owns one Bot instance per connection. Telegram acknowledges an
update only when ``getUpdates`` is called with a larger offset, so the offset is
persisted only after the inbound event has reached the durable ChannelStore.
A crash in between causes redelivery, which the store's deterministic inbound
claim resolves to the same Run.

Sends are at-most-once at the host boundary: the ChannelManager delivery
receipt prevents a replay, and this adapter never retries a send whose remote
outcome is unknown. Only a definite 429 (Telegram refused the call) is retried
with the advertised delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import Message, Update

from cairn.channels.adapter import AdapterFactory, InboundSink
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelAdapterHealth,
    ChannelConnection,
    ChannelCredentialError,
    ChannelInboundConflict,
    ChannelInboundEvent,
    ChannelInboundRejected,
    ChannelInboundUncertain,
    ChannelNotFound,
    ChannelPlatform,
    ChannelSendResult,
    ChannelSendStatus,
    utc_now,
)

TOKEN_RE = re.compile(r"^\d{1,20}:[A-Za-z0-9_-]{20,}$")
MESSAGE_LIMIT = 4000  # Telegram's hard limit is 4096 UTF-16 code units.
POLL_TIMEOUT = 25
MAX_RETRY_AFTER = 30.0
NETWORK_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

# One Bot Token may only own one long-polling loop in this process. Starting a
# second connection with the same token would make Telegram answer 409 and the
# two connections would steal each other's updates.
_ACTIVE_TOKENS: dict[str, str] = {}


class AdapterStateStore(Protocol):
    def load_adapter_state(
        self, connection_id: str, key: str
    ) -> dict[str, object] | None: ...

    def save_adapter_state(
        self, connection_id: str, key: str, value: dict[str, object]
    ) -> None: ...


BotFactory = Callable[[str], Bot]


def _default_bot(token: str) -> Bot:
    return Bot(token=token, session=AiohttpSession(timeout=POLL_TIMEOUT + 10))


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> tuple[str, ...]:
    """Split text into Telegram-sized chunks without breaking a surrogate pair."""
    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if _utf16_length(remaining) <= limit:
            chunks.append(remaining)
            break
        candidate = remaining[:limit]
        while candidate and _utf16_length(candidate) > limit:
            candidate = candidate[:-1]
        if not candidate:
            candidate = remaining[:1]
        newline = candidate.rfind("\n")
        space = candidate.rfind(" ")
        if newline >= limit // 2:
            chunk = candidate[:newline]
        elif space >= limit // 2:
            chunk = candidate[:space]
        else:
            chunk = candidate
        chunk = chunk.rstrip()
        if not chunk:
            chunk = candidate
        chunks.append(chunk)
        remaining = remaining[len(chunk) :].lstrip()
    return tuple(chunks)


class TelegramAdapter:
    """Long-polling Telegram Bot bound to one stored Channel connection."""

    platform = ChannelPlatform.TELEGRAM
    externally_managed = False

    def __init__(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
        *,
        store: AdapterStateStore,
        bot_factory: BotFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.connection_id = connection.connection_id
        self._credentials = dict(credentials or {})
        self._inbound = inbound
        self._store = store
        self._bot_factory = bot_factory or _default_bot
        self._sleep = sleep
        self._bot: Bot | None = None
        self._bot_id: int | None = None
        self._offset = self._load_offset()
        self._state = ChannelActualState.STOPPED
        self._last_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._last_error: str | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._reserved = False

    def _load_offset(self) -> int | None:
        value = self._store.load_adapter_state(self.connection_id, "telegram")
        if value is None:
            return None
        offset = value.get("offset")
        return offset if isinstance(offset, int) and offset >= 0 else None

    def _save_offset(self) -> None:
        self._store.save_adapter_state(
            self.connection_id, "telegram", {"offset": self._offset}
        )

    def _token(self) -> str:
        token = self._credentials.get("bot_token", "").strip()
        if not token:
            raise ChannelCredentialError("telegram_token_missing")
        if not TOKEN_RE.fullmatch(token):
            raise ChannelCredentialError("telegram_token_invalid")
        return token

    def _reserve_token(self, token: str) -> None:
        owner = _ACTIVE_TOKENS.get(token)
        if owner is not None and owner != self.connection_id:
            raise ChannelCredentialError("telegram_token_in_use")
        _ACTIVE_TOKENS[token] = self.connection_id
        self._reserved = True

    def _release_token(self) -> None:
        token = self._credentials.get("bot_token", "").strip()
        if self._reserved and _ACTIVE_TOKENS.get(token) == self.connection_id:
            _ACTIVE_TOKENS.pop(token, None)
        self._reserved = False

    def _safe_error(self, exc: BaseException) -> str:
        token = self._credentials.get("bot_token", "").strip()
        message = f"{type(exc).__name__}: {exc}"
        if token:
            message = message.replace(token, "<redacted>")
        return message[:300]

    async def start(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        if self._bot is not None:
            await self._close_bot(self._bot)
            self._bot = None
        self._release_token()
        try:
            token = self._token()
        except ChannelCredentialError as exc:
            self._state = ChannelActualState.ERROR
            self._last_error = str(exc)
            raise
        self._reserve_token(token)
        self._state = ChannelActualState.CONNECTING
        self._last_error = None
        bot: Bot | None = None
        try:
            bot = self._bot_factory(token)
            me = await bot.get_me()
        except (TelegramUnauthorizedError, TelegramNotFound, TelegramBadRequest) as exc:
            await self._close_bot(bot)
            self._release_token()
            self._state = ChannelActualState.ERROR
            self._last_error = "telegram_invalid_token"
            raise ChannelCredentialError("telegram_invalid_token") from exc
        except Exception as exc:
            await self._close_bot(bot)
            self._release_token()
            self._state = ChannelActualState.ERROR
            self._last_error = self._safe_error(exc)
            raise ChannelAdapterError(self._last_error) from exc
        self._bot = bot
        self._bot_id = me.id
        self._state = ChannelActualState.RUNNING
        self._last_connected_at = utc_now()
        self._last_heartbeat_at = self._last_connected_at
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        bot, self._bot = self._bot, None
        self._bot_id = None
        await self._close_bot(bot)
        self._release_token()
        self._state = ChannelActualState.STOPPED
        self._last_heartbeat_at = None

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    def health(self) -> ChannelAdapterHealth:
        return ChannelAdapterHealth(
            actual_state=self._state,
            last_connected_at=self._last_connected_at,
            last_heartbeat_at=self._last_heartbeat_at,
            last_error=self._last_error,
        )

    async def _close_bot(self, bot: Bot | None) -> None:
        if bot is None:
            return
        with contextlib.suppress(Exception):
            await bot.session.close()

    async def _poll_loop(self) -> None:
        failures = 0
        while True:
            bot = self._bot
            if bot is None:
                return
            try:
                updates = await bot.get_updates(
                    offset=self._offset,
                    timeout=POLL_TIMEOUT,
                    allowed_updates=["message"],
                    request_timeout=POLL_TIMEOUT + 10,
                )
            except asyncio.CancelledError:
                raise
            except TelegramRetryAfter as exc:
                self._last_error = (
                    f"telegram_rate_limited: retry after {exc.retry_after}"
                )
                await self._sleep(min(float(exc.retry_after), MAX_RETRY_AFTER))
                continue
            except TelegramConflictError:
                self._state = ChannelActualState.ERROR
                self._last_error = "telegram_conflict: another poller uses this token"
                return
            except TelegramUnauthorizedError:
                self._state = ChannelActualState.ERROR
                self._last_error = "telegram_invalid_token"
                return
            except (
                TelegramNetworkError,
                TelegramServerError,
                OSError,
                TimeoutError,
            ) as exc:
                failures += 1
                delay = NETWORK_BACKOFF[min(failures - 1, len(NETWORK_BACKOFF) - 1)]
                self._state = ChannelActualState.CONNECTING
                self._last_error = self._safe_error(exc)
                await self._sleep(delay)
                continue
            except Exception as exc:
                self._state = ChannelActualState.ERROR
                self._last_error = self._safe_error(exc)
                return
            failures = 0
            self._state = ChannelActualState.RUNNING
            self._last_error = None
            self._last_heartbeat_at = utc_now()
            try:
                for update in updates:
                    await self._handle_update(update)
            except ChannelInboundUncertain as exc:
                # The update is not acknowledged; Telegram will redeliver it.
                # The inbound claim and deterministic request ID make the retry
                # resolve to the same Run instead of executing twice.
                self._last_error = f"inbound_uncertain: {exc}"
                await self._sleep(1.0)
            except Exception as exc:
                self._state = ChannelActualState.ERROR
                self._last_error = self._safe_error(exc)
                return

    async def _handle_update(self, update: Update) -> None:
        message = update.message
        if (
            message is not None
            and message.chat.type == "private"
            and message.text
            and message.text.strip()
            and message.from_user is not None
            and not message.from_user.is_bot
            and message.from_user.id != self._bot_id
        ):
            event = ChannelInboundEvent(
                platform=ChannelPlatform.TELEGRAM,
                connection_id=self.connection_id,
                external_message_id=(f"{message.chat.id}:{message.message_id}"),
                sender_id=str(message.from_user.id),
                conversation_binding=str(message.chat.id),
                text=message.text,
                received_at=utc_now(),
            )
            try:
                await self._inbound(event)
            except (ChannelInboundRejected, ChannelInboundConflict, ChannelNotFound):
                # A definite refusal is still acknowledged: replaying it would
                # only repeat the same rejection.
                pass
            except ChannelInboundUncertain:
                raise
        self._offset = update.update_id + 1
        self._save_offset()

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        if self._state is not ChannelActualState.RUNNING or self._bot is None:
            raise ChannelAdapterError("telegram adapter is not running")
        try:
            chat_id = int(target)
        except ValueError as exc:
            raise ChannelAdapterError("telegram target is not a chat id") from exc
        chunks = split_message(message)
        for index, chunk in enumerate(chunks):
            result = await self._send_chunk(chat_id, chunk)
            if result.status is not ChannelSendStatus.ACCEPTED:
                if index:
                    return ChannelSendResult(
                        status=ChannelSendStatus.UNKNOWN,
                        error=f"telegram partial send: {result.error}",
                    )
                return result
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)

    async def _send_chunk(self, chat_id: int, chunk: str) -> ChannelSendResult:
        try:
            await self._send_with_markdown(chat_id, chunk)
        except TelegramRetryAfter as exc:
            await self._sleep(min(float(exc.retry_after), MAX_RETRY_AFTER))
            try:
                await self._send_with_markdown(chat_id, chunk)
            except Exception as retry:
                return self._send_failure(retry)
        except Exception as exc:
            return self._send_failure(exc)
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)

    async def _send_with_markdown(self, chat_id: int, chunk: str) -> Message:
        bot = self._bot
        if bot is None:
            raise ChannelAdapterError("telegram adapter is not running")
        try:
            return await bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except TelegramBadRequest as exc:
            if not _is_entity_error(exc):
                raise
            # Model output often contains unmatched Markdown. Deliver the same
            # text as plain text instead of losing the reply to formatting.
            return await bot.send_message(chat_id, chunk)

    def _send_failure(self, exc: BaseException) -> ChannelSendResult:
        error = self._safe_error(exc)
        if isinstance(
            exc,
            (
                TelegramBadRequest,
                TelegramForbiddenError,
                TelegramNotFound,
                TelegramUnauthorizedError,
            ),
        ):
            return ChannelSendResult(status=ChannelSendStatus.REJECTED, error=error)
        return ChannelSendResult(status=ChannelSendStatus.UNKNOWN, error=error)


def _is_entity_error(exc: TelegramBadRequest) -> bool:
    description = str(exc).lower()
    return "parse entities" in description or "can't find end" in description


def telegram_adapter_factory(
    store: AdapterStateStore,
    *,
    bot_factory: BotFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AdapterFactory:
    def build(
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
    ) -> TelegramAdapter:
        return TelegramAdapter(
            connection,
            credentials,
            inbound,
            store=store,
            bot_factory=bot_factory,
            sleep=sleep,
        )

    return build


__all__ = [
    "TelegramAdapter",
    "split_message",
    "telegram_adapter_factory",
]
