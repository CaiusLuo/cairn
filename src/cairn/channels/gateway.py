"""Host-owned Gateway sessions for Discord and QQ."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx2 as httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.protocol import State

from cairn.channels.adapter import InboundSink
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelAdapterHealth,
    ChannelConnection,
    ChannelInboundConflict,
    ChannelInboundEvent,
    ChannelInboundPaused,
    ChannelInboundRejected,
    ChannelNotFound,
    ChannelPlatform,
    utc_now,
)

_RETRY_SECONDS = (2, 5, 10, 20, 40, 60)
_ACTIVE_BOTS: set[tuple[ChannelPlatform, str]] = set()
# Gateway frame debugging prints IDENTIFY and RESUME credentials.
_WIRE_LOG = logging.Logger("cairn.channels.gateway")
_WIRE_LOG.disabled = True


class GatewaySocket(Protocol):
    @property
    def state(self) -> State: ...

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


GatewayOpen = Callable[[str], AbstractAsyncContextManager[GatewaySocket]]


class _DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # Credentials are sent to the URL validated during Gateway discovery.
        return exc


def open_gateway(url: str) -> AbstractAsyncContextManager[GatewaySocket]:
    return _DirectConnect(
        url,
        open_timeout=8,
        close_timeout=2,
        ping_interval=None,
        max_size=1024 * 1024,
        proxy=None,
        logger=_WIRE_LOG,
    )


def gateway_url(value: object, domains: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ChannelAdapterError("gateway_url_invalid")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme == "wss"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and any(host == domain or host.endswith("." + domain) for domain in domains)
        ):
            return value
    except ValueError:
        pass
    raise ChannelAdapterError("gateway_url_invalid")


@dataclass(slots=True)
class GatewaySession:
    session_id: str
    bot_id: str
    sequence: int
    resume_url: str | None


class _Reconnect(Exception):
    pass


class GatewayAdapter(ABC):
    externally_managed = False
    platform: ChannelPlatform
    fatal_close_codes: frozenset[int] = frozenset(
        {4001, 4002, 4010, 4011, 4012, 4013, 4014}
    )

    def __init__(
        self,
        connection: ChannelConnection,
        inbound: InboundSink,
        *,
        socket: GatewayOpen = open_gateway,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.connection_id = connection.connection_id
        self._inbound = inbound
        self._open = socket
        self._transport = transport
        self._state = ChannelActualState.STOPPED
        self._error: str | None = None
        self._connected: str | None = None
        self._heartbeat: str | None = None
        self._session: GatewaySession | None = None
        self._reserved: str | None = None
        self._worker: asyncio.Task[None] | None = None
        self._socket: GatewaySocket | None = None
        self._started = asyncio.Event()
        self._http: httpx.AsyncClient | None = None

    def health(self) -> ChannelAdapterHealth:
        state = self._state
        if state is ChannelActualState.RUNNING and self.identity() is None:
            state = ChannelActualState.CONNECTING
        return ChannelAdapterHealth(
            actual_state=state,
            last_connected_at=self._connected,
            last_heartbeat_at=self._heartbeat,
            last_error=self._error,
        )

    def identity(self) -> str | None:
        if (
            self._state is ChannelActualState.RUNNING
            and self._session
            and self._socket
            and self._socket.state is State.OPEN
        ):
            return self._session.bot_id
        return None

    async def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        self._state = ChannelActualState.CONNECTING
        self._started.clear()
        self._error = None
        self._worker = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._started.wait(), timeout=25)
        except asyncio.CancelledError:
            await self.stop()
            raise
        except TimeoutError:
            await self.stop()
            self._state = ChannelActualState.ERROR
            self._error = "gateway_start_timeout"
            raise ChannelAdapterError(self._error) from None
        if self._state is not ChannelActualState.RUNNING:
            raise ChannelAdapterError(self._error or "gateway_unavailable")

    async def stop(self) -> None:
        self._state = ChannelActualState.STOPPED
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._started.set()
        self._error = self._heartbeat = None

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    async def _run(self) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=10,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as http:
                self._http = http
                failures = 0
                while True:
                    start = asyncio.get_running_loop().time()
                    try:
                        await self._connect()
                    except ConnectionClosed as exc:
                        code = exc.rcvd.code if exc.rcvd else None
                        if code in self.fatal_close_codes:
                            raise ChannelAdapterError(
                                f"gateway_closed_{code}"
                            ) from None
                        self._on_close(code)
                    except (_Reconnect, WebSocketException, OSError, TimeoutError):
                        pass
                    if not self._started.is_set():
                        raise ChannelAdapterError("gateway_unavailable")
                    if asyncio.get_running_loop().time() - start >= 60:
                        failures = 0
                    self._state = ChannelActualState.CONNECTING
                    self._error = "gateway_reconnecting"
                    await asyncio.sleep(_RETRY_SECONDS[failures])
                    failures = min(failures + 1, len(_RETRY_SECONDS) - 1)
        except ChannelAdapterError as exc:
            self._error = str(exc)
            self._state = ChannelActualState.ERROR
        except Exception:
            self._error = "gateway_failed"
            self._state = ChannelActualState.ERROR
        finally:
            self._http = self._socket = self._session = None
            if self._reserved is not None:
                _ACTIVE_BOTS.discard((self.platform, self._reserved))
                self._reserved = None
            self._started.set()

    async def _connect(self) -> None:
        prior = self._session
        try:
            url, auth = await self._handshake(prior)
        except httpx.HTTPError:
            raise _Reconnect from None
        async with self._open(url) as socket:
            self._socket = socket
            try:
                await self._listen(socket, prior, auth)
            finally:
                self._socket = None
                if self._state is not ChannelActualState.STOPPED:
                    self._state = ChannelActualState.CONNECTING
                    # A normal close invalidates resumable Gateway sessions.
                    await socket.close(code=4000)

    async def _listen(
        self,
        socket: GatewaySocket,
        session: GatewaySession | None,
        auth: dict[str, object],
    ) -> None:
        hello = await self._read(socket, 8)
        data = hello.get("d")
        if hello.get("op") != 10 or not isinstance(data, dict):
            raise ChannelAdapterError("gateway_hello_invalid")
        interval_ms = data.get("heartbeat_interval")
        if (
            not isinstance(interval_ms, (int, float))
            or isinstance(interval_ms, bool)
            or not 0 < interval_ms <= 120000
        ):
            raise ChannelAdapterError("gateway_interval_invalid")
        interval = interval_ms / 1000
        await socket.send(json.dumps(auth))
        clock = asyncio.get_running_loop().time
        next_beat = clock() + random.random() * interval
        auth_deadline = clock() + 12
        awaiting_ack = False
        received = session.sequence if session else None
        replay: list[tuple[int, ChannelInboundEvent]] = []
        while True:
            running = self._state is ChannelActualState.RUNNING
            now = clock()
            if not running and now >= auth_deadline:
                raise _Reconnect
            if now >= next_beat:
                if awaiting_ack:
                    raise _Reconnect
                await socket.send(json.dumps({"op": 1, "d": received}))
                awaiting_ack = True
                next_beat = now + interval
            deadline = next_beat if running else min(next_beat, auth_deadline)
            try:
                frame = await self._read(socket, max(0, deadline - clock()))
            except TimeoutError:
                continue
            op = frame.get("op")
            if op == 11:
                awaiting_ack = False
                self._heartbeat = utc_now()
            elif op == 1:
                await socket.send(json.dumps({"op": 1, "d": received}))
                awaiting_ack = True
            elif op in (7, 9):
                if op == 9 and frame.get("d") is not True:
                    self._session = None
                raise _Reconnect
            elif op == 0:
                kind, body, seq = frame.get("t"), frame.get("d"), frame.get("s")
                if not isinstance(kind, str) or type(seq) is not int or seq < 0:
                    raise ChannelAdapterError("gateway_dispatch_invalid")
                received = seq
                if (
                    kind == "READY"
                    and session is None
                    and not running
                    and isinstance(body, dict)
                ):
                    session = self._register(body, seq)
                    self._authenticated()
                elif kind == "RESUMED" and session is not None and not running:
                    self._authenticated()
                    for item_seq, replayed in replay:
                        await self._admit(replayed)
                        session.sequence = item_seq
                    replay.clear()
                elif kind in {"READY", "RESUMED"}:
                    raise ChannelAdapterError("gateway_session_invalid")
                elif session is not None and isinstance(body, dict):
                    event = self._message(kind, body, session.bot_id)
                    if event is not None:
                        if running:
                            await self._admit(event)
                        elif len(replay) < 128:
                            replay.append((seq, event))
                        else:
                            raise ChannelAdapterError("gateway_resume_buffer_full")
                if session is not None and self._state is ChannelActualState.RUNNING:
                    session.sequence = max(session.sequence, seq)

    @staticmethod
    async def _read(socket: GatewaySocket, timeout: float) -> dict[str, object]:
        async with asyncio.timeout(timeout):
            frame = json.loads(await socket.recv())
        if not isinstance(frame, dict):
            raise ChannelAdapterError("gateway_frame_invalid")
        return frame

    def _register(self, data: dict[str, object], seq: int) -> GatewaySession:
        session = self._ready(data, seq)
        if self._reserved is not None and self._reserved != session.bot_id:
            raise ChannelAdapterError("gateway_bot_identity_changed")
        key = (self.platform, session.bot_id)
        if self._reserved is None:
            if key in _ACTIVE_BOTS:
                raise ChannelAdapterError("gateway_bot_already_running")
            _ACTIVE_BOTS.add(key)
            self._reserved = session.bot_id
        self._session = session
        return session

    def _authenticated(self) -> None:
        self._state = ChannelActualState.RUNNING
        self._connected = utc_now()
        self._error = None
        self._started.set()

    async def _admit(self, event: ChannelInboundEvent) -> None:
        try:
            await self._inbound(event)
        except ChannelInboundPaused:
            raise _Reconnect from None
        except (ChannelInboundRejected, ChannelInboundConflict, ChannelNotFound):
            pass
        except Exception:
            # Preserve the last settled sequence so the Gateway can replay.
            raise _Reconnect from None

    def _on_close(self, code: int | None) -> None:
        if code in (4004, 4007, 4009):
            self._session = None

    @abstractmethod
    async def _handshake(
        self,
        session: GatewaySession | None,
    ) -> tuple[str, dict[str, object]]: ...

    @abstractmethod
    def _ready(self, data: dict[str, object], seq: int) -> GatewaySession: ...

    @abstractmethod
    def _message(
        self,
        kind: str,
        data: dict[str, object],
        bot_id: str,
    ) -> ChannelInboundEvent | None: ...
