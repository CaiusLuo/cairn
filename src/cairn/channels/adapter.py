"""Minimal adapter contract and the C0 fake adapter.

A real platform SDK belongs in C1-C4. This module only fixes the boundary the
manager depends on: lifecycle, health, outbound send and an inbound callback.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelAdapterHealth,
    ChannelAdapterNotImplemented,
    ChannelConnection,
    ChannelCredentialError,
    ChannelInboundEvent,
    ChannelPlatform,
    ChannelSendResult,
    ChannelSendStatus,
    utc_now,
)

InboundSink = Callable[[ChannelInboundEvent], Awaitable[object]]
AdapterFactory = Callable[
    [ChannelConnection, "dict[str, str] | None", InboundSink], "ChannelAdapter"
]


class ChannelAdapterRegistry:
    """Platform-to-factory registry.

    Production starts empty and reports ``adapter_not_implemented`` for every
    stored connection. Only tests inject the fake adapter explicitly; C1-C4
    register real adapters here.
    """

    def __init__(self) -> None:
        self._factories: dict[ChannelPlatform, AdapterFactory] = {}

    def register(
        self, platform: ChannelPlatform, factory: AdapterFactory
    ) -> ChannelAdapterRegistry:
        self._factories[platform] = factory
        return self

    def supports(self, platform: ChannelPlatform) -> bool:
        return platform in self._factories

    def build(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
    ) -> ChannelAdapter:
        factory = self._factories.get(connection.platform)
        if factory is None:
            raise ChannelAdapterNotImplemented(
                f"adapter_not_implemented: {connection.platform.value}"
            )
        return factory(connection, credentials, inbound)


class ChannelAdapter(Protocol):
    connection_id: str
    platform: ChannelPlatform
    externally_managed: bool

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def reconnect(self) -> None: ...

    def health(self) -> ChannelAdapterHealth: ...

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult: ...


class FakeChannelAdapter:
    """Deterministic adapter for C0 lifecycle/API tests and dry-run wiring."""

    externally_managed = False

    def __init__(
        self,
        connection: ChannelConnection,
        inbound: InboundSink,
        credentials: dict[str, str] | None = None,
    ) -> None:
        self.connection_id = connection.connection_id
        self.platform = connection.platform
        self._inbound = inbound
        self._credentials = dict(credentials or {})
        self._state = ChannelActualState.STOPPED
        self._last_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._last_error: str | None = None
        self.fail_start = False
        self.fail_send = False
        self.starts = 0
        self.stops = 0
        self.reconnects = 0
        self.sent: list[tuple[str, str, str]] = []

    async def start(self) -> None:
        self.starts += 1
        if self.fail_start:
            self._state = ChannelActualState.ERROR
            self._last_error = "fake_start_failed"
            raise ChannelCredentialError("fake adapter rejected its credentials")
        self._state = ChannelActualState.RUNNING
        self._last_connected_at = utc_now()
        self._last_heartbeat_at = self._last_connected_at
        self._last_error = None

    async def stop(self) -> None:
        self.stops += 1
        self._state = ChannelActualState.STOPPED
        self._last_heartbeat_at = None

    async def reconnect(self) -> None:
        self.reconnects += 1
        await self.stop()
        await self.start()

    def health(self) -> ChannelAdapterHealth:
        return ChannelAdapterHealth(
            actual_state=self._state,
            last_connected_at=self._last_connected_at,
            last_heartbeat_at=self._last_heartbeat_at,
            last_error=self._last_error,
        )

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        if self._state is not ChannelActualState.RUNNING:
            raise ChannelAdapterError("adapter is not running")
        if self.fail_send:
            raise ChannelAdapterError("fake adapter could not confirm the send")
        self.sent.append((target, message, idempotency_key))
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)

    async def emit_inbound(self, event: ChannelInboundEvent) -> object:
        return await self._inbound(event)


__all__ = [
    "AdapterFactory",
    "ChannelAdapter",
    "ChannelAdapterRegistry",
    "FakeChannelAdapter",
    "InboundSink",
]
