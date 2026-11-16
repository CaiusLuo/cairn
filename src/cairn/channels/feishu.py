"""Feishu self-built application adapter using the official lark-oapi channel.

The SDK is imported at module load, before the Server event loop starts. Its
WebSocket client captures a process-global asyncio loop at import time, so
importing it lazily from inside a running loop would bind the SDK to the
Server loop and make the blocking ``start()`` unusable. For the same reason
one Server process owns at most one Feishu WebSocket connection; a second
connection is refused explicitly instead of competing for that loop.

Inbound identity is the SDK-resolved bot open_id scoped by the configured app
ID. It is never derived from message text. The ChannelStore uses it to scope
inbound dedupe and reply intents, so replacing the application identity opens
a new namespace while old pending replies fail closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

# Imported eagerly on purpose; see the module docstring.
from lark_oapi.channel import (
    DedupConfig,
    FeishuChannel,
    InboundConfig,
    OutboundConfig,
    PolicyConfig,
    RetryConfig,
    SafetyConfig,
    SendOpts,
    TextBatchConfig,
)

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

MESSAGE_LIMIT = 3500  # Feishu's hard limit is much higher; keep replies small.
START_TIMEOUT = 20.0
IDENTITY_TIMEOUT = 10.0
INBOUND_RETRY_SECONDS = 2.0
INBOUND_RETRY_WINDOW = 300.0
MAX_DEFERRED_INBOUND = 32
MAX_RETRY_AFTER = 30.0
STALE_WINDOW_MS = 24 * 60 * 60 * 1000

_APP_ID = re.compile(r"^[A-Za-z0-9_-]{3,200}$")
_DEFINITE_SEND_FAILURES = {
    "format_error",
    "target_revoked",
    "permission_denied",
}


class FeishuIdentityProtocol(Protocol):
    open_id: str


class FeishuChannelProtocol(Protocol):
    @property
    def bot_identity(self) -> FeishuIdentityProtocol | None: ...

    @property
    def is_ready(self) -> bool: ...

    async def start_background(self, *, timeout: float | None = 30.0) -> None: ...

    async def stop_background(self) -> None: ...

    async def resolve_bot_identity(self) -> FeishuIdentityProtocol | None: ...

    def on(self, name: str, handler: Callable[..., object]) -> Callable[[], None]: ...

    async def send(
        self, to: object, message: object, opts: object | None = None
    ) -> object: ...


ChannelFactory = Callable[[str, str], FeishuChannelProtocol]


def _default_channel(app_id: str, app_secret: str) -> FeishuChannelProtocol:
    channel = FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        transport="ws",
        # Cairn owns sender authorization. The SDK must still deliver every
        # private text event; its in-memory dedupe and batching would otherwise
        # hide redeliveries from the durable ChannelStore.
        policy=PolicyConfig(dm_policy="open"),
        inbound=InboundConfig(drop_self_sent=True),
        safety=SafetyConfig(
            dedup=DedupConfig(enabled=False),
            text_batch=TextBatchConfig(delay_ms=0, max_messages=1),
            stale_message_window_ms=STALE_WINDOW_MS,
        ),
        # One attempt only: an UNKNOWN outcome must never be replayed by the
        # SDK. Cairn records it as an at-most-once delivery instead.
        outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)),
    )
    return cast(FeishuChannelProtocol, channel)


@dataclass
class _Reservations:
    app_connections: dict[str, str]
    ws_connection: str | None


_ACTIVE = _Reservations(app_connections={}, ws_connection=None)


@dataclass(frozen=True, slots=True)
class _DeferredInbound:
    event: ChannelInboundEvent
    deadline: float


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> tuple[str, ...]:
    """Split text into Feishu-sized chunks, preferring line or word breaks."""
    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        candidate = remaining[:limit]
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


def _chunk_uuid(idempotency_key: str, index: int) -> str:
    raw = f"{idempotency_key}\0{index}".encode()
    return hashlib.sha256(raw).hexdigest()[:40]


class FeishuAdapter:
    """One self-built Feishu app connected over a WebSocket long connection."""

    platform = ChannelPlatform.FEISHU
    externally_managed = False

    def __init__(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
        *,
        channel_factory: ChannelFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.connection_id = connection.connection_id
        self._credentials = dict(credentials or {})
        self._inbound = inbound
        self._channel_factory = channel_factory or _default_channel
        self._sleep = sleep
        self._channel: FeishuChannelProtocol | None = None
        self._identity: str | None = None
        self._bot_open_id: str | None = None
        self._state = ChannelActualState.STOPPED
        self._last_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._last_error: str | None = None
        self._closing = False
        self._deferred: list[_DeferredInbound] = []
        self._worker: asyncio.Task[None] | None = None
        self._reserved_app: str | None = None
        self._reserved_ws = False

    def _credentials_value(self) -> tuple[str, str]:
        app_id = self._credentials.get("app_id", "").strip()
        app_secret = self._credentials.get("app_secret", "").strip()
        if not app_id:
            raise ChannelCredentialError("feishu_app_id_missing")
        if not _APP_ID.fullmatch(app_id):
            raise ChannelCredentialError("feishu_app_id_invalid")
        if not app_secret:
            raise ChannelCredentialError("feishu_app_secret_missing")
        if len(app_secret) > 500:
            raise ChannelCredentialError("feishu_app_secret_invalid")
        return app_id, app_secret

    def _reserve(self, app_id: str) -> None:
        owner = _ACTIVE.app_connections.get(app_id)
        if owner is not None and owner != self.connection_id:
            raise ChannelCredentialError("feishu_app_in_use")
        if (
            _ACTIVE.ws_connection is not None
            and _ACTIVE.ws_connection != self.connection_id
        ):
            # lark-oapi's WebSocket client uses a process-global event loop, so
            # a second concurrent connection in this process cannot start.
            raise ChannelCredentialError("feishu_ws_busy")
        _ACTIVE.app_connections[app_id] = self.connection_id
        _ACTIVE.ws_connection = self.connection_id
        self._reserved_app = app_id
        self._reserved_ws = True

    def _release(self) -> None:
        if (
            self._reserved_app is not None
            and _ACTIVE.app_connections.get(self._reserved_app) == self.connection_id
        ):
            _ACTIVE.app_connections.pop(self._reserved_app, None)
        if self._reserved_ws and _ACTIVE.ws_connection == self.connection_id:
            _ACTIVE.ws_connection = None
        self._reserved_app = None
        self._reserved_ws = False

    def _safe_error(self, exc: BaseException) -> str:
        secret = self._credentials.get("app_secret", "").strip()
        message = f"{type(exc).__name__}: {exc}"
        if secret:
            message = message.replace(secret, "<redacted>")
        return message[:300]

    def _safe_text(self, value: object) -> str:
        secret = self._credentials.get("app_secret", "").strip()
        message = str(value)
        if secret:
            message = message.replace(secret, "<redacted>")
        return message[:300]

    async def start(self) -> None:
        if self._state is ChannelActualState.RUNNING and self._channel is not None:
            return
        if self._channel is not None:
            # A previous instance was not fully stopped. Never open a second
            # transport while the old one may still be alive.
            await self.stop()
        self._closing = False
        try:
            app_id, app_secret = self._credentials_value()
        except ChannelCredentialError as exc:
            self._state = ChannelActualState.ERROR
            self._last_error = str(exc)
            raise
        try:
            self._reserve(app_id)
        except ChannelCredentialError as exc:
            self._state = ChannelActualState.ERROR
            self._last_error = str(exc)
            raise
        self._state = ChannelActualState.CONNECTING
        self._last_error = None
        channel: FeishuChannelProtocol | None = None
        try:
            channel = self._channel_factory(app_id, app_secret)
            channel.on("message", self._handle_message)
            channel.on("reconnecting", self._on_reconnecting)
            channel.on("reconnected", self._on_reconnected)
            self._channel = channel
            await channel.start_background(timeout=START_TIMEOUT)
            identity = channel.bot_identity
            if identity is None:
                try:
                    identity = await asyncio.wait_for(
                        channel.resolve_bot_identity(), timeout=IDENTITY_TIMEOUT
                    )
                except TimeoutError as exc:
                    raise ChannelAdapterError("feishu_identity_timeout") from exc
            open_id = str(getattr(identity, "open_id", "") or "").strip()
            if not open_id:
                raise ChannelAdapterError(
                    "feishu_identity_unavailable: the bot identity could not be resolved"
                )
            self._bot_open_id = open_id
            self._identity = f"feishu:{app_id}:{open_id}"
            self._state = ChannelActualState.RUNNING
            self._last_connected_at = utc_now()
            self._last_heartbeat_at = self._last_connected_at
            self._last_error = None
        except asyncio.CancelledError:
            await self._abort_start(channel)
            raise
        except ChannelCredentialError as exc:
            await self._abort_start(channel)
            self._last_error = str(exc)
            self._state = ChannelActualState.ERROR
            raise
        except Exception as exc:
            self._last_error = self._start_error(exc)
            await self._abort_start(channel)
            self._state = ChannelActualState.ERROR
            raise ChannelAdapterError(self._last_error) from exc

    def _start_error(self, exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "feishu_start_timeout"
        return self._safe_error(exc)

    async def _abort_start(self, channel: FeishuChannelProtocol | None) -> None:
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.stop_background()
        self._channel = None
        self._release()
        self._identity = None
        self._bot_open_id = None
        self._deferred.clear()
        self._worker = None

    async def stop(self) -> None:
        self._closing = True
        channel, self._channel = self._channel, None
        if channel is not None:
            try:
                await channel.stop_background()
            except Exception as exc:
                self._channel = channel
                self._state = ChannelActualState.ERROR
                self._last_error = self._safe_error(exc)
                raise ChannelAdapterError(self._last_error) from exc
        self._release()
        self._identity = None
        self._bot_open_id = None
        self._deferred.clear()
        self._worker = None
        self._state = ChannelActualState.STOPPED
        self._last_heartbeat_at = None
        # ``_closing`` stays set until the next start so a deferred-admission
        # worker left on the SDK loop exits instead of racing a new identity.

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

    def identity(self) -> str | None:
        return self._identity

    def _on_reconnecting(self) -> None:
        if self._state is ChannelActualState.RUNNING:
            self._state = ChannelActualState.CONNECTING
            self._last_error = "feishu_reconnecting"

    def _on_reconnected(self) -> None:
        if self._state is ChannelActualState.CONNECTING:
            self._state = ChannelActualState.RUNNING
            self._last_error = None
            self._last_heartbeat_at = utc_now()

    async def _handle_message(self, inbound: object) -> None:
        event = self._normalise(inbound)
        if event is None:
            return
        if self._deferred or self._state is not ChannelActualState.RUNNING:
            # Preserve per-connection order while the head is waiting for an
            # active Run to finish, and hold messages that arrive during a
            # reconnect instead of dropping an event the SDK has already ACKed.
            self._defer(event)
            return
        if not await self._admit(event):
            self._defer(event)

    async def _admit(self, event: ChannelInboundEvent) -> bool:
        """Try durable admission; return False only for a retryable outcome."""
        try:
            await self._inbound(event)
        except (ChannelInboundRejected, ChannelInboundConflict, ChannelNotFound):
            # A definite refusal is final; replaying it only repeats the answer.
            return True
        except ChannelInboundUncertain as exc:
            self._last_error = f"inbound_uncertain: {self._safe_error(exc)}"[:300]
            return False
        except Exception as exc:
            self._last_error = self._safe_error(exc)
            return True
        self._last_heartbeat_at = utc_now()
        return True

    def _normalise(self, inbound: object) -> ChannelInboundEvent | None:
        if self._state not in {
            ChannelActualState.RUNNING,
            ChannelActualState.CONNECTING,
        }:
            return None
        if self._identity is None:
            return None
        conversation = getattr(inbound, "conversation", None)
        sender = getattr(inbound, "sender", None)
        content = getattr(inbound, "content", None)
        chat_type = getattr(conversation, "chat_type", None)
        if chat_type != "p2p":
            return None
        if bool(getattr(sender, "is_bot", False)):
            return None
        sender_id = getattr(sender, "open_id", None)
        if not isinstance(sender_id, str) or not sender_id.strip():
            return None
        if self._bot_open_id is not None and sender_id == self._bot_open_id:
            return None
        chat_id = getattr(conversation, "chat_id", None)
        message_id = getattr(inbound, "id", None)
        # Only text content is in scope. Media and rich posts normalize to a
        # different content type and are ignored rather than forwarded as
        # placeholder text.
        text = getattr(content, "text", None)
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(chat_id, str)
            or not chat_id.strip()
            or not isinstance(text, str)
            or not text.strip()
        ):
            return None
        return ChannelInboundEvent(
            platform=ChannelPlatform.FEISHU,
            connection_id=self.connection_id,
            external_message_id=message_id,
            sender_id=sender_id,
            conversation_binding=chat_id,
            text=text,
            received_at=utc_now(),
            adapter_identity=self._identity,
        )

    def _defer(self, event: ChannelInboundEvent) -> None:
        if len(self._deferred) >= MAX_DEFERRED_INBOUND:
            self._last_error = "feishu_inbound_deferred_full"
            return
        deadline = asyncio.get_running_loop().time() + INBOUND_RETRY_WINDOW
        self._deferred.append(_DeferredInbound(event=event, deadline=deadline))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain_deferred())

    async def _drain_deferred(self) -> None:
        current = asyncio.current_task()
        try:
            while self._deferred and not self._closing and self._worker is current:
                pending = self._deferred[0]
                if self._state is not ChannelActualState.RUNNING:
                    # A reconnect or stop may be in progress. Keep waiting while
                    # the identity is still valid; ``stop`` clears this queue.
                    if asyncio.get_running_loop().time() >= pending.deadline:
                        self._drop_head(pending)
                        self._last_error = "feishu_inbound_deferred_expired"
                        continue
                    await self._sleep(INBOUND_RETRY_SECONDS)
                    continue
                try:
                    await self._inbound(pending.event)
                except (
                    ChannelInboundRejected,
                    ChannelInboundConflict,
                    ChannelNotFound,
                ):
                    pass
                except ChannelInboundUncertain as exc:
                    self._last_error = f"inbound_deferred: {self._safe_error(exc)}"[
                        :300
                    ]
                    if asyncio.get_running_loop().time() >= pending.deadline:
                        self._drop_head(pending)
                        self._last_error = (
                            f"inbound_deferred_expired: {self._safe_error(exc)}"[:300]
                        )
                        continue
                    await self._sleep(INBOUND_RETRY_SECONDS)
                    continue
                except Exception as exc:
                    self._last_error = self._safe_error(exc)
                else:
                    self._last_heartbeat_at = utc_now()
                self._drop_head(pending)
        finally:
            if self._worker is current:
                self._worker = None
            if not self._deferred and (self._last_error or "").startswith(
                "inbound_deferred"
            ):
                self._last_error = None

    def _drop_head(self, pending: _DeferredInbound) -> None:
        # ``stop`` may clear the list while the worker is awaiting admission.
        if self._deferred and self._deferred[0] is pending:
            self._deferred.pop(0)

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        channel = self._channel
        if self._state is not ChannelActualState.RUNNING or channel is None:
            raise ChannelAdapterError("feishu adapter is not running")
        if not target.strip():
            raise ChannelAdapterError("feishu target is empty")
        if not message.strip():
            raise ChannelAdapterError("feishu message is empty")
        chunks = split_message(message)
        for index, chunk in enumerate(chunks):
            result = await self._send_chunk(
                channel, target, chunk, _chunk_uuid(idempotency_key, index)
            )
            if result.status is not ChannelSendStatus.ACCEPTED:
                if index:
                    return ChannelSendResult(
                        status=ChannelSendStatus.UNKNOWN,
                        error=f"feishu_partial_send: {result.error}",
                    )
                return result
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)

    async def _send_chunk(
        self,
        channel: FeishuChannelProtocol,
        target: str,
        chunk: str,
        uuid: str,
    ) -> ChannelSendResult:
        for attempt in range(2):
            try:
                raw = await channel.send(target, chunk, SendOpts(uuid=uuid))
            except Exception as exc:
                result = self._failure_from_exception(exc)
                if result.status is ChannelSendStatus.REJECTED:
                    return result
                if attempt == 0 and self._is_rate_limited_exception(exc):
                    await self._sleep(self._retry_after(exc))
                    continue
                return result
            if bool(getattr(raw, "success", False)):
                return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)
            error = getattr(raw, "error", None)
            if error is None:
                return ChannelSendResult(
                    status=ChannelSendStatus.UNKNOWN,
                    error="feishu_unknown: send returned no result",
                )
            code = self._error_code(error)
            text = self._error_text(error, code)
            if code == "rate_limited" and attempt == 0:
                await self._sleep(self._retry_after(error))
                continue
            if code in _DEFINITE_SEND_FAILURES:
                return ChannelSendResult(status=ChannelSendStatus.REJECTED, error=text)
            return ChannelSendResult(status=ChannelSendStatus.UNKNOWN, error=text)
        return ChannelSendResult(
            status=ChannelSendStatus.UNKNOWN,
            error="feishu_rate_limited: retry did not succeed",
        )

    def _failure_from_exception(self, exc: BaseException) -> ChannelSendResult:
        code = self._error_code(getattr(exc, "code", None))
        text = f"feishu_{code}: {self._safe_error(exc)}"
        if code in _DEFINITE_SEND_FAILURES:
            return ChannelSendResult(status=ChannelSendStatus.REJECTED, error=text)
        return ChannelSendResult(status=ChannelSendStatus.UNKNOWN, error=text)

    def _is_rate_limited_exception(self, exc: BaseException) -> bool:
        return self._error_code(getattr(exc, "code", None)) == "rate_limited"

    @staticmethod
    def _error_code(error: object) -> str:
        # Accept either an error object or an already-extracted code enum.
        code = getattr(error, "code", error)
        value = getattr(code, "value", code)
        return str(value or "unknown")

    def _error_text(self, error: object, code: str) -> str:
        hint = getattr(error, "hint", None)
        text = f"feishu_{code}" + (f": {hint}" if hint else "")
        return self._safe_text(text)

    @staticmethod
    def _retry_after(error: object) -> float:
        value = getattr(error, "retry_after_seconds", None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(1.0, min(float(value), MAX_RETRY_AFTER))
        return 1.0


def feishu_adapter_factory(
    *,
    channel_factory: ChannelFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AdapterFactory:
    def build(
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
    ) -> FeishuAdapter:
        return FeishuAdapter(
            connection,
            credentials,
            inbound,
            channel_factory=channel_factory,
            sleep=sleep,
        )

    return build


__all__ = [
    "FeishuAdapter",
    "feishu_adapter_factory",
    "split_message",
]
