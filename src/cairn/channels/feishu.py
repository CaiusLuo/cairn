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

The SDK's WebSocket client ACKs a DATA frame immediately after its synchronous
dispatcher returns; the dispatcher only schedules the channel-level message
pipeline on the SDK background loop. Cairn therefore installs a best-effort
pre-ACK hook that persists a private-text envelope into the SQLite inbox before
the ACK is written. The normal async handler writes the same envelope again
(idempotent), and a Server-loop drain worker performs admission. If the
process exits after the ACK, pending envelopes are recovered and admitted on
the next start instead of depending on a platform redelivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

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
    ChannelInboundInboxEntry,
    ChannelInboundPaused,
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
MAX_INBOX_ATTEMPTS = 150
MAX_PENDING_INBOX = 512
DRAIN_BATCH = 64
DRAIN_STOP_TIMEOUT = 5.0
PERSIST_RETRIES = 3
PERSIST_RETRY_DELAY = 0.05
MAX_RETRY_AFTER = 30.0
STALE_WINDOW_MS = 24 * 60 * 60 * 1000

_APP_ID = re.compile(r"^[A-Za-z0-9_-]{3,200}$")
_DEFINITE_SEND_FAILURES = {
    "format_error",
    "target_revoked",
    "permission_denied",
}
_INBOX_ERROR_PREFIXES = ("feishu_inbound", "inbound_uncertain")


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


class FeishuInboxProtocol(Protocol):
    """Durable inbound envelopes owned by the ChannelStore."""

    def enqueue_inbound_inbox(
        self,
        event: ChannelInboundEvent,
        *,
        pending_limit: int,
        window_seconds: float,
    ) -> tuple[bool, str]: ...

    def due_inbound_inbox(
        self,
        connection_id: str,
        adapter_identity: str,
        *,
        limit: int = 64,
    ) -> tuple[ChannelInboundInboxEntry, ...]: ...

    def has_pending_inbound_inbox(
        self, connection_id: str, adapter_identity: str
    ) -> bool: ...

    def record_inbound_inbox_attempt(
        self,
        connection_id: str,
        adapter_identity: str,
        external_message_id: str,
        *,
        error: str,
        next_attempt_at: float,
        increment: bool = True,
    ) -> None: ...

    def settle_inbound_inbox(
        self,
        connection_id: str,
        adapter_identity: str,
        external_message_id: str,
        *,
        state: str,
        error: str | None = None,
    ) -> bool: ...

    def fail_inbound_inbox_other_identity(
        self,
        connection_id: str,
        adapter_identity: str,
        *,
        reason: str,
    ) -> int: ...


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
        inbox: FeishuInboxProtocol,
        channel_factory: ChannelFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_seconds: float = INBOUND_RETRY_SECONDS,
        window_seconds: float = INBOUND_RETRY_WINDOW,
        max_attempts: int = MAX_INBOX_ATTEMPTS,
        max_pending: int = MAX_PENDING_INBOX,
    ) -> None:
        self.connection_id = connection.connection_id
        self._credentials = dict(credentials or {})
        self._inbound = inbound
        self._inbox = inbox
        self._channel_factory = channel_factory or _default_channel
        self._sleep = sleep
        self._retry_seconds = retry_seconds
        self._window_seconds = window_seconds
        self._max_attempts = max_attempts
        self._max_pending = max_pending
        self._channel: FeishuChannelProtocol | None = None
        self._identity: str | None = None
        self._bot_open_id: str | None = None
        self._state = ChannelActualState.STOPPED
        self._last_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._last_error: str | None = None
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._pre_ack_active = False
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
            self._isolate_other_identity()
            self._loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
            self._pre_ack_active = True
            self._install_pre_ack_hook(channel)
            self._drain_task = asyncio.create_task(self._drain_loop())
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
        self._closing = True
        self._pre_ack_active = False
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.stop_background()
        await self._stop_drain(grace=0.0)
        self._channel = None
        self._release()
        self._identity = None
        self._bot_open_id = None

    async def _stop_drain(self, *, grace: float) -> None:
        task, self._drain_task = self._drain_task, None
        self._wake_worker()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=grace)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        self._wake = None
        self._loop = None

    async def stop(self) -> None:
        self._closing = True
        channel, self._channel = self._channel, None
        failure: Exception | None = None
        if channel is not None:
            try:
                await channel.stop_background()
            except Exception as exc:
                failure = exc
        if failure is not None:
            # The transport may still be delivering after a failed stop. Keep
            # the pre-ACK hook active so anything acknowledged during that
            # window is persisted for the next start, and leave the drain
            # stopped so nothing is admitted under an Error connection.
            await self._stop_drain(grace=0.0)
            self._channel = channel
            self._state = ChannelActualState.ERROR
            self._last_error = self._safe_error(failure)
            raise ChannelAdapterError(self._last_error) from failure
        # Stop the transport before deactivating the pre-ACK hook so an event
        # already acknowledged during shutdown still reaches the inbox.
        self._pre_ack_active = False
        await self._stop_drain(grace=DRAIN_STOP_TIMEOUT)
        self._release()
        self._identity = None
        self._bot_open_id = None
        self._state = ChannelActualState.STOPPED
        self._last_heartbeat_at = None
        # Pending inbox rows are deliberately retained. The next start drains
        # them after re-checking the current sender authorization and identity.

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
            self._wake_worker()

    def _isolate_other_identity(self) -> None:
        identity = self._identity
        if identity is None:
            return
        try:
            failed = self._inbox.fail_inbound_inbox_other_identity(
                self.connection_id,
                identity,
                reason="feishu_inbound_identity_changed",
            )
        except Exception as exc:
            self._last_error = (
                f"feishu_inbound_inbox_unavailable: {self._safe_error(exc)}"[:300]
            )
            return
        if failed:
            self._last_error = (
                f"feishu_inbound_identity_changed: {failed} pending message(s) isolated"
            )[:300]

    def _install_pre_ack_hook(self, channel: FeishuChannelProtocol) -> None:
        """Persist a private-text envelope before the SDK writes its ACK.

        ``lark-oapi``'s WS client invokes the dispatcher synchronously and ACKs
        immediately after it returns; the channel-level ``message`` handler is
        only scheduled on the SDK background loop. Wrapping the private
        dispatcher method closes the ACK-to-handler crash window. This is
        deliberately best-effort: a missing or changed SDK attribute leaves the
        normal handler and durable admission in place.
        """
        ws_client = getattr(channel, "_ws_client", None)
        dispatcher = getattr(ws_client, "_event_handler", None)
        original = getattr(dispatcher, "_do_without_validation", None)
        if dispatcher is None or not callable(original):
            return

        def dispatch_with_durability(payload: Any) -> Any:
            try:
                self._persist_raw_payload(payload)
            except Exception as exc:
                self._last_error = (
                    f"feishu_inbound_preack_failed: {self._safe_error(exc)}"[:300]
                )
            return original(payload)

        try:
            dispatcher._do_without_validation = dispatch_with_durability
        except (AttributeError, TypeError):
            return

    def _persist_raw_payload(self, payload: Any) -> None:
        if not self._pre_ack_active:
            return
        event = self._raw_inbound(payload)
        if event is not None:
            self._enqueue(event)

    def _raw_inbound(self, payload: Any) -> ChannelInboundEvent | None:
        """Minimal p2 ``im.message.receive_v1`` parser for the pre-ACK hook.

        The async SDK pipeline still performs the authoritative normalization;
        this parser only covers the scope Cairn accepts (private human text)
        and returns ``None`` for anything else so the normal handler remains the
        filter of record.
        """
        identity = self._identity
        if identity is None:
            return None
        if isinstance(payload, bytes):
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
        else:
            raw = payload
        if not isinstance(raw, dict):
            return None
        header = raw.get("header")
        if not isinstance(header, dict):
            return None
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = raw.get("event")
        if not isinstance(event, dict):
            return None
        message = event.get("message")
        sender = event.get("sender")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return None
        if message.get("chat_type") != "p2p":
            return None
        if message.get("message_type") != "text":
            return None
        if sender.get("sender_type") not in (None, "user"):
            return None
        sender_ids = sender.get("sender_id")
        sender_id = sender_ids.get("open_id") if isinstance(sender_ids, dict) else None
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        content = message.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                return None
        text = content.get("text") if isinstance(content, dict) else None
        if (
            not isinstance(sender_id, str)
            or not sender_id.strip()
            or not isinstance(message_id, str)
            or not message_id.strip()
            or not isinstance(chat_id, str)
            or not chat_id.strip()
            or not isinstance(text, str)
            or not text.strip()
        ):
            return None
        if self._bot_open_id is not None and sender_id == self._bot_open_id:
            return None
        if self._is_stale(message.get("create_time")):
            return None
        return ChannelInboundEvent(
            platform=ChannelPlatform.FEISHU,
            connection_id=self.connection_id,
            external_message_id=message_id,
            sender_id=sender_id,
            conversation_binding=chat_id,
            text=text,
            received_at=utc_now(),
            adapter_identity=identity,
        )

    @staticmethod
    def _is_stale(create_time: object) -> bool:
        if isinstance(create_time, bool) or not isinstance(create_time, (int, str)):
            return False
        try:
            created_ms = int(create_time)
        except (TypeError, ValueError):
            return False
        if created_ms <= 0:
            return False
        return bool(created_ms < (time.time() * 1000) - STALE_WINDOW_MS)

    async def _handle_message(self, inbound: object) -> None:
        event = self._normalise(inbound)
        if event is None:
            return
        state = self._enqueue(event)
        if state is None:
            for _ in range(PERSIST_RETRIES - 1):
                await self._sleep(PERSIST_RETRY_DELAY)
                state = self._enqueue(event)
                if state is not None:
                    break
        if state is None:
            self._last_error = "feishu_inbound_persist_failed"
            return
        if state == "pending" and not self._closing:
            self._wake_worker()

    def _enqueue(self, event: ChannelInboundEvent) -> str | None:
        try:
            _, state = self._inbox.enqueue_inbound_inbox(
                event,
                pending_limit=self._max_pending,
                window_seconds=self._window_seconds,
            )
        except Exception as exc:
            self._last_error = f"feishu_inbound_persist_retry: {self._safe_error(exc)}"[
                :300
            ]
            return None
        if state == "conflict":
            self._last_error = "feishu_inbound_id_conflict"
        elif state == "failed":
            self._last_error = "feishu_inbound_backpressure"
        return state

    def _wake_worker(self) -> None:
        loop = self._loop
        wake = self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            return

    def _is_closing(self) -> bool:
        return self._closing

    async def _drain_loop(self) -> None:
        try:
            while not self._closing:
                identity = self._identity
                if identity is None:
                    return
                if self._state is not ChannelActualState.RUNNING:
                    await self._wait_for_work()
                    continue
                try:
                    items = self._inbox.due_inbound_inbox(
                        self.connection_id, identity, limit=DRAIN_BATCH
                    )
                except Exception as exc:
                    self._last_error = (
                        f"feishu_inbound_inbox_unavailable: {self._safe_error(exc)}"[
                            :300
                        ]
                    )
                    await self._sleep(self._retry_seconds)
                    continue
                if not items:
                    await self._wait_for_work()
                    continue
                for item in items:
                    if self._is_closing():
                        return
                    try:
                        await self._process_inbox(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error = (
                            f"feishu_inbound_drain_error: {self._safe_error(exc)}"[:300]
                        )
                        await self._sleep(self._retry_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"feishu_inbound_drain_stopped: {self._safe_error(exc)}"[
                :300
            ]

    async def _wait_for_work(self) -> None:
        wake = self._wake
        identity = self._identity
        if wake is None or identity is None:
            return
        try:
            if self._inbox.has_pending_inbound_inbox(self.connection_id, identity):
                await asyncio.wait_for(wake.wait(), timeout=self._retry_seconds)
            else:
                await wake.wait()
        except TimeoutError:
            pass
        finally:
            if not self._closing:
                wake.clear()

    async def _process_inbox(self, item: ChannelInboundInboxEntry) -> None:
        try:
            await self._inbound(item.event)
        except ChannelInboundPaused as exc:
            error = f"feishu_inbound_paused: {self._safe_error(exc)}"[:300]
            self._last_error = error
            self._record_attempt(item, error, increment=False)
            return
        except (ChannelInboundRejected, ChannelInboundConflict) as exc:
            self._settle(item, "rejected", self._safe_error(exc))
            return
        except ChannelNotFound as exc:
            self._settle(item, "failed", self._safe_error(exc))
            return
        except ChannelInboundUncertain as exc:
            self._record_attempt(item, self._safe_error(exc), increment=True)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A plain adapter/store failure is not an acceptance. Keep the
            # envelope pending so the next attempt reconciles the same claim.
            self._record_attempt(item, self._safe_error(exc), increment=True)
            return
        self._settle(item, "admitted")
        self._last_heartbeat_at = utc_now()
        if (self._last_error or "").startswith(_INBOX_ERROR_PREFIXES):
            self._last_error = None

    def _record_attempt(
        self, item: ChannelInboundInboxEntry, error: str, *, increment: bool
    ) -> None:
        self._last_error = f"feishu_inbound_retry: {error}"[:300]
        attempts = item.attempts + 1 if increment else item.attempts
        now = time.time()
        if increment and (attempts >= self._max_attempts or now >= item.deadline_at):
            self._settle(item, "failed", f"feishu_inbound_expired: {error}"[:300])
            return
        try:
            self._inbox.record_inbound_inbox_attempt(
                self.connection_id,
                item.event.adapter_identity or "",
                item.event.external_message_id,
                error=error,
                next_attempt_at=now + self._retry_seconds,
                increment=increment,
            )
        except Exception as exc:
            self._last_error = (
                f"feishu_inbound_inbox_unavailable: {self._safe_error(exc)}"[:300]
            )

    def _settle(
        self,
        item: ChannelInboundInboxEntry,
        state: str,
        error: str | None = None,
    ) -> None:
        try:
            self._inbox.settle_inbound_inbox(
                self.connection_id,
                item.event.adapter_identity or "",
                item.event.external_message_id,
                state=state,
                error=error,
            )
        except Exception as exc:
            self._last_error = (
                f"feishu_inbound_inbox_unavailable: {self._safe_error(exc)}"[:300]
            )
        else:
            if state == "admitted" and (self._last_error or "").startswith(
                _INBOX_ERROR_PREFIXES
            ):
                self._last_error = None

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
    inbox: FeishuInboxProtocol,
    *,
    channel_factory: ChannelFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retry_seconds: float = INBOUND_RETRY_SECONDS,
    window_seconds: float = INBOUND_RETRY_WINDOW,
    max_attempts: int = MAX_INBOX_ATTEMPTS,
    max_pending: int = MAX_PENDING_INBOX,
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
            inbox=inbox,
            channel_factory=channel_factory,
            sleep=sleep,
            retry_seconds=retry_seconds,
            window_seconds=window_seconds,
            max_attempts=max_attempts,
            max_pending=max_pending,
        )

    return build


__all__ = [
    "FeishuAdapter",
    "feishu_adapter_factory",
    "split_message",
]
