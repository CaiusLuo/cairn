"""Per-connection channel lifecycle with bounded recovery and durable receipts."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cairn.channels.adapter import (
    ChannelAdapter,
    ChannelAdapterRegistry,
)
from cairn.channels.models import (
    ALL_PLATFORMS,
    ChannelActualState,
    ChannelAdapterError,
    ChannelAdapterHealth,
    ChannelAdapterNotImplemented,
    ChannelConfirmationRequired,
    ChannelConnection,
    ChannelDeliveryUncertain,
    ChannelDesiredState,
    ChannelDisabled,
    ChannelDisplayState,
    ChannelInboundConflict,
    ChannelInboundEvent,
    ChannelInboundRejected,
    ChannelInboundResult,
    ChannelInboundUncertain,
    ChannelLegacyManaged,
    ChannelNotFound,
    ChannelPlatform,
    ChannelPlatformSummary,
    ChannelRevisionConflict,
    ChannelRuntimeStatus,
    ChannelSendResult,
    ChannelSendStatus,
)
from cairn.channels.store import (
    ChannelStore,
    CreateResult,
    DeleteResult,
    RequestClaim,
    UpdateResult,
    inbound_digest,
)
from cairn.conversation.models import Conversation, ConversationSource, Run
from cairn.shared.lifecycle import settle_owned
from cairn.shared.redaction import redact_text


class ChannelIntake(Protocol):
    """The subset of ConversationService the channel core needs."""

    def ensure_channel_conversation(
        self,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
        initial_prompt: str,
    ) -> Conversation: ...

    def submit(
        self,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        *,
        model_id: str | None = None,
    ) -> Run: ...

    def get_channel_conversation(
        self,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
    ) -> Conversation | None: ...

    def run_by_request(self, conversation_id: str, request_id: str) -> Run | None: ...


@dataclass(frozen=True, slots=True)
class ChannelConnectionView:
    connection: ChannelConnection
    runtime: ChannelRuntimeStatus


@dataclass
class _Runtime:
    adapter: ChannelAdapter | None = None
    retry_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inbound_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attempts: int = 0
    last_error: str | None = None
    next_retry_at: str | None = None
    secrets: tuple[str, ...] = ()


def _future(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _not_implemented(platform: ChannelPlatform) -> str:
    return f"adapter_not_implemented: {platform.value}"


class ChannelManager:
    """One isolated lifecycle per connection; configuration alone is silent."""

    def __init__(
        self,
        *,
        store: ChannelStore,
        conversations: ChannelIntake,
        adapters: ChannelAdapterRegistry | None = None,
        retry_delays: tuple[float, ...] = (2, 4, 8, 16, 32, 60),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        secret_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        if not retry_delays or any(delay < 0 for delay in retry_delays):
            raise ValueError("retry_delays must be non-negative")
        self.store = store
        self.conversations = conversations
        # Production starts with an empty registry: the fake adapter is only
        # ever injected by tests, and C1-C4 register real factories here.
        self._adapters = adapters if adapters is not None else ChannelAdapterRegistry()
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._secret_sink = secret_sink
        self._runtimes: dict[str, _Runtime] = {}
        self._legacy: ChannelAdapter | None = None
        self._stopping = False
        self._started = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Channel manager already started")
        self._started = True
        self._stopping = False
        # A send that was "sending" when the process stopped has an unknown
        # remote result and must never be replayed as a fresh send.
        self.store.mark_inflight_deliveries_unknown()
        for connection in self.store.list_connections():
            self._runtime(connection.connection_id)
            if (
                connection.enabled
                and connection.desired_state is ChannelDesiredState.RUNNING
            ):
                self._schedule_retry(connection.connection_id, immediate=True)

    async def close(self) -> None:
        self._stopping = True
        tasks = [
            runtime.retry_task
            for runtime in self._runtimes.values()
            if runtime.retry_task is not None
        ]
        if tasks:
            await settle_owned(*tasks)
        for runtime in self._runtimes.values():
            runtime.retry_task = None
        adapters = [
            runtime.adapter
            for runtime in self._runtimes.values()
            if runtime.adapter is not None and not runtime.adapter.externally_managed
        ]
        for adapter in adapters:
            with contextlib.suppress(Exception):
                await adapter.stop()
        self._runtimes.clear()
        self._started = False

    def attach_legacy(self, adapter: ChannelAdapter) -> None:
        if not adapter.externally_managed:
            raise ValueError("legacy channel adapter must be externally managed")
        if adapter.platform is not ChannelPlatform.WECHAT:
            raise ValueError("only WeChat can be a legacy channel")
        if self._legacy is not None:
            raise RuntimeError("legacy channel adapter already attached")
        self._legacy = adapter
        runtime = self._runtime(adapter.connection_id)
        values = getattr(adapter, "secret_values", None)
        if callable(values):
            secrets = tuple(str(value) for value in values() if value)
            runtime.secrets = secrets
            if secrets and self._secret_sink is not None:
                self._secret_sink(secrets)

    def set_secret_sink(self, sink: Callable[[tuple[str, ...]], None]) -> None:
        self._secret_sink = sink
        for runtime in self._runtimes.values():
            if runtime.secrets:
                sink(runtime.secrets)

    def _runtime(self, connection_id: str) -> _Runtime:
        return self._runtimes.setdefault(connection_id, _Runtime())

    def _legacy_view(self) -> ChannelConnectionView | None:
        if self._legacy is None:
            return None
        connection = getattr(self._legacy, "connection", None)
        if not callable(connection):
            return None
        value = connection()
        if value is None:
            return None
        return ChannelConnectionView(
            connection=value, runtime=self._runtime_status(value)
        )

    def _stored_view(self, connection: ChannelConnection) -> ChannelConnectionView:
        return ChannelConnectionView(
            connection=connection,
            runtime=self._runtime_status(connection),
        )

    def list_connection_views(
        self, platform: ChannelPlatform | None = None
    ) -> tuple[ChannelConnectionView, ...]:
        views: list[ChannelConnectionView] = []
        legacy = self._legacy_view()
        legacy_id = legacy.connection.connection_id if legacy is not None else None
        if legacy is not None and (
            platform is None or legacy.connection.platform is platform
        ):
            views.append(legacy)
        for connection in self.store.list_connections(platform):
            if connection.connection_id == legacy_id:
                continue
            views.append(self._stored_view(connection))
        return tuple(views)

    def list_connections(
        self, platform: ChannelPlatform | None = None
    ) -> tuple[ChannelConnection, ...]:
        return tuple(view.connection for view in self.list_connection_views(platform))

    def platform_summaries(
        self, platform: ChannelPlatform | None = None
    ) -> tuple[ChannelPlatformSummary, ...]:
        views = self.list_connection_views(platform)
        result: list[ChannelPlatformSummary] = []
        for candidate in ALL_PLATFORMS:
            if platform is not None and candidate is not platform:
                continue
            rows = [view for view in views if view.connection.platform is candidate]
            running = sum(
                1
                for view in rows
                if view.runtime.actual_state is ChannelActualState.RUNNING
            )
            if not rows:
                state = ChannelDisplayState.UNCONFIGURED
            elif running:
                state = ChannelDisplayState.RUNNING
            else:
                state = ChannelDisplayState.SILENT
            result.append(
                ChannelPlatformSummary(
                    platform=candidate,
                    display_state=state,
                    connections=len(rows),
                    running=running,
                )
            )
        return tuple(result)

    def get_connection_view(self, connection_id: str) -> ChannelConnectionView:
        if self._legacy is not None and connection_id == self._legacy.connection_id:
            legacy = self._legacy_view()
            if legacy is not None:
                return legacy
            raise ChannelNotFound(connection_id)
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ChannelNotFound(connection_id)
        return self._stored_view(connection)

    def get_runtime_status(self, connection_id: str) -> ChannelRuntimeStatus:
        if self._legacy is not None and connection_id == self._legacy.connection_id:
            legacy = self._legacy_view()
            if legacy is None:
                raise ChannelNotFound(connection_id)
            return legacy.runtime
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ChannelNotFound(connection_id)
        return self._runtime_status(connection)

    def _runtime_status(self, connection: ChannelConnection) -> ChannelRuntimeStatus:
        runtime = self._runtime(connection.connection_id)
        adapter = runtime.adapter
        if adapter is None and connection.legacy:
            adapter = self._legacy
        if adapter is not None:
            health = adapter.health()
            return ChannelRuntimeStatus(
                connection_id=connection.connection_id,
                actual_state=health.actual_state,
                last_connected_at=health.last_connected_at,
                last_heartbeat_at=health.last_heartbeat_at,
                last_error=health.last_error or runtime.last_error,
                next_retry_at=runtime.next_retry_at,
            )
        return ChannelRuntimeStatus(
            connection_id=connection.connection_id,
            actual_state=ChannelActualState.STOPPED,
            last_error=runtime.last_error,
            next_retry_at=runtime.next_retry_at,
        )

    async def create_connection(
        self,
        *,
        platform: ChannelPlatform,
        display_name: str,
        enabled: bool,
        owner_identity: str | None,
        metadata: dict[str, object],
        credentials: dict[str, str] | None,
        request_id: str,
        allowed_senders: Sequence[str] = (),
    ) -> ChannelConnectionView:
        if platform is ChannelPlatform.WECHAT:
            raise ChannelLegacyManaged(
                "WeChat is managed by the existing single legacy adapter"
            )
        result: CreateResult = self.store.create_connection(
            platform=platform,
            display_name=display_name,
            enabled=enabled,
            owner_identity=owner_identity,
            metadata=metadata,
            credentials=credentials,
            request_id=request_id,
            allowed_senders=allowed_senders,
        )
        return self._stored_view(result.connection)

    async def update_connection(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        request_id: str,
        changes: dict[str, object],
        credentials: dict[str, str] | None = None,
        replace_credentials: bool = False,
    ) -> ChannelConnectionView:
        self._require_stored(connection_id)
        result: UpdateResult = self.store.update_connection(
            connection_id,
            expected_revision=expected_revision,
            request_id=request_id,
            changes=changes,
            credentials=credentials,
            replace_credentials=replace_credentials,
        )
        if result.replayed:
            return self._stored_view(result.connection)
        runtime = self._runtime(connection_id)
        if not result.connection.enabled:
            await self._cancel_retry(connection_id)
            try:
                await self._stop_locked(runtime)
            except Exception as exc:
                runtime.last_error = self._safe_error(exc, runtime)
            else:
                runtime.last_error = None
                self._drop_adapter(connection_id)
        elif result.credentials_replaced:
            await self._cancel_retry(connection_id)
            try:
                await self._stop_locked(runtime)
            except Exception as exc:
                # Keep the old adapter referenced rather than starting a second
                # instance that could open another session for the same account.
                runtime.last_error = self._safe_error(exc, runtime)
                return self._stored_view(result.connection)
            runtime.last_error = None
            self._drop_adapter(connection_id)
            if result.connection.desired_state is ChannelDesiredState.RUNNING:
                view = await self._try_start(result.connection)
                if view.runtime.actual_state is not ChannelActualState.RUNNING:
                    self._schedule_retry(connection_id)
                return view
        return self._stored_view(result.connection)

    async def delete_connection(
        self,
        connection_id: str,
        *,
        expected_revision: int,
        request_id: str,
        confirmed: bool,
    ) -> DeleteResult:
        if not confirmed:
            raise ChannelConfirmationRequired(
                "deleting a channel connection requires confirmation"
            )
        if self._legacy is not None and connection_id == self._legacy.connection_id:
            raise ChannelLegacyManaged("legacy connections are host-managed")
        current = self.store.get_connection(connection_id)
        if current is None:
            # A completed delete replay is still idempotent after the row is gone.
            return self.store.delete_connection(
                connection_id,
                expected_revision=expected_revision,
                request_id=request_id,
                confirmed=confirmed,
            )
        if current.revision != expected_revision:
            raise ChannelRevisionConflict(current.revision)
        runtime = self._runtime(connection_id)
        await self._cancel_retry(connection_id)
        try:
            await self._stop_locked(runtime)
        except Exception:
            # Deleting the config while its adapter may still run would orphan
            # network work; leave the connection intact so the host can retry.
            raise ChannelAdapterError(
                "channel adapter could not be stopped; connection was not deleted"
            ) from None
        self._drop_adapter(connection_id)
        result = self.store.delete_connection(
            connection_id,
            expected_revision=expected_revision,
            request_id=request_id,
            confirmed=confirmed,
        )
        self._runtimes.pop(connection_id, None)
        return result

    async def start_connection(
        self, connection_id: str, request_id: str
    ) -> ChannelConnectionView:
        view = self.get_connection_view(connection_id)
        if view.connection.legacy:
            assert self._legacy is not None
            claim = self.store.claim_request(
                request_id,
                operation="legacy-start",
                digest_value={"connection_id": connection_id},
            )
            if claim.replayed:
                return self._view_from_receipt(claim, connection_id)
            runtime = self._runtime(connection_id)
            try:
                await self._legacy.start()
            except Exception as exc:
                runtime.last_error = self._safe_error(exc, runtime)
                result = self._view_payload(self.get_connection_view(connection_id))
                self.store.complete_request(request_id, result)
                return self.get_connection_view(connection_id)
            runtime.last_error = None
            result = self._view_payload(self.get_connection_view(connection_id))
            self.store.complete_request(request_id, result)
            return self.get_connection_view(connection_id)
        if not view.connection.enabled:
            raise ChannelDisabled("channel connection is disabled")
        if not self._adapters.supports(view.connection.platform):
            raise ChannelAdapterNotImplemented(
                _not_implemented(view.connection.platform)
            )
        claim = self.store.claim_request(
            request_id,
            operation="start",
            digest_value={"connection_id": connection_id},
        )
        if claim.replayed:
            return self._view_from_receipt(claim, connection_id)
        await self._cancel_retry(connection_id)
        current = self.store.set_desired_state(
            connection_id, ChannelDesiredState.RUNNING
        )
        view = await self._try_start(current)
        if view.runtime.actual_state is not ChannelActualState.RUNNING:
            self._schedule_retry(connection_id)
        self.store.complete_request(request_id, self._view_payload(view))
        return view

    async def stop_connection(
        self, connection_id: str, request_id: str
    ) -> ChannelConnectionView:
        view = self.get_connection_view(connection_id)
        if view.connection.legacy:
            assert self._legacy is not None
            claim = self.store.claim_request(
                request_id,
                operation="legacy-stop",
                digest_value={"connection_id": connection_id},
            )
            if claim.replayed:
                return self._view_from_receipt(claim, connection_id)
            try:
                await self._legacy.stop()
            except Exception as exc:
                runtime = self._runtime(connection_id)
                runtime.last_error = self._safe_error(exc, runtime)
            else:
                self._runtime(connection_id).last_error = None
            result = self._view_payload(self.get_connection_view(connection_id))
            self.store.complete_request(request_id, result)
            return self.get_connection_view(connection_id)
        claim = self.store.claim_request(
            request_id,
            operation="stop",
            digest_value={"connection_id": connection_id},
        )
        if claim.replayed:
            return self._view_from_receipt(claim, connection_id)
        current = self.store.set_desired_state(
            connection_id, ChannelDesiredState.STOPPED
        )
        await self._cancel_retry(connection_id)
        runtime = self._runtime(connection_id)
        try:
            await self._stop_locked(runtime)
        except Exception as exc:
            runtime.last_error = self._safe_error(exc, runtime)
        else:
            runtime.last_error = None
        view = self._stored_view(current)
        self.store.complete_request(request_id, self._view_payload(view))
        return view

    async def reconnect_connection(
        self, connection_id: str, request_id: str
    ) -> ChannelConnectionView:
        view = self.get_connection_view(connection_id)
        if view.connection.legacy:
            assert self._legacy is not None
            claim = self.store.claim_request(
                request_id,
                operation="legacy-reconnect",
                digest_value={"connection_id": connection_id},
            )
            if claim.replayed:
                return self._view_from_receipt(claim, connection_id)
            runtime = self._runtime(connection_id)
            try:
                await self._legacy.reconnect()
            except Exception as exc:
                runtime.last_error = self._safe_error(exc, runtime)
            else:
                runtime.last_error = None
            result = self._view_payload(self.get_connection_view(connection_id))
            self.store.complete_request(request_id, result)
            return self.get_connection_view(connection_id)
        if not view.connection.enabled:
            raise ChannelDisabled("channel connection is disabled")
        if not self._adapters.supports(view.connection.platform):
            raise ChannelAdapterNotImplemented(
                _not_implemented(view.connection.platform)
            )
        claim = self.store.claim_request(
            request_id,
            operation="reconnect",
            digest_value={"connection_id": connection_id},
        )
        if claim.replayed:
            return self._view_from_receipt(claim, connection_id)
        await self._cancel_retry(connection_id)
        current = self.store.set_desired_state(
            connection_id, ChannelDesiredState.RUNNING
        )
        runtime = self._runtime(connection_id)
        runtime.attempts = 0
        async with runtime.lock:
            try:
                adapter = self._adapter_for(current)
                await adapter.reconnect()
                runtime.last_error = None
                runtime.next_retry_at = None
            except Exception as exc:
                runtime.last_error = self._safe_error(exc, runtime)
                runtime.attempts += 1
                self._schedule_retry(connection_id)
        view = self._stored_view(self.store.get_connection(connection_id) or current)
        self.store.complete_request(request_id, self._view_payload(view))
        return view

    async def send(
        self,
        connection_id: str,
        target: str,
        message: str,
        idempotency_key: str,
    ) -> ChannelSendResult:
        view = self.get_connection_view(connection_id)
        if not view.connection.enabled:
            raise ChannelDisabled("channel connection is disabled")
        if not target.strip() or not message.strip():
            raise ValueError("target and message must be non-empty")
        adapter = self._adapter_for_view(view)
        if adapter.health().actual_state is not ChannelActualState.RUNNING:
            raise ChannelAdapterError("channel adapter is not running")
        delivery, created = self.store.begin_delivery(
            connection_id=connection_id,
            platform=view.connection.platform,
            target=target,
            message=message,
            idempotency_key=idempotency_key,
        )
        if not created:
            if delivery.status == ChannelSendStatus.ACCEPTED.value:
                return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)
            if delivery.status == ChannelSendStatus.REJECTED.value:
                return ChannelSendResult(
                    status=ChannelSendStatus.REJECTED, error=delivery.error
                )
            raise ChannelDeliveryUncertain(
                "channel delivery has an unknown or in-flight outcome"
            )
        try:
            result = await adapter.send(target, message, idempotency_key)
        except asyncio.CancelledError:
            self.store.complete_delivery(
                delivery.delivery_id, ChannelSendStatus.UNKNOWN, "cancelled"
            )
            raise
        except Exception as exc:
            runtime = self._runtime(connection_id)
            error = self._safe_error(exc, runtime)
            self.store.complete_delivery(
                delivery.delivery_id, ChannelSendStatus.UNKNOWN, error
            )
            return ChannelSendResult(status=ChannelSendStatus.UNKNOWN, error=error)
        self.store.complete_delivery(delivery.delivery_id, result.status, result.error)
        return result

    async def accept_inbound(self, event: ChannelInboundEvent) -> ChannelInboundResult:
        connection = self._view_for_inbound(event.connection_id)
        if not connection.enabled:
            raise ChannelInboundRejected("channel connection is disabled")
        if connection.platform is not event.platform:
            raise ChannelInboundConflict("inbound platform does not match connection")
        # Authorization precedes every durable side effect. An owner or an
        # explicitly allowed sender is required; an empty allowlist denies all
        # non-owner identities.
        if not self._sender_authorized(connection, event.sender_id):
            raise ChannelInboundRejected("sender is not authorized")
        if not self._adapter_running(connection):
            raise ChannelInboundRejected("channel connection is not running")
        runtime = self._runtime(event.connection_id)
        # Serialize one connection's intake so a duplicate delivery cannot
        # race the first submission. A "claimed" row left by a process
        # interruption is retried with the same deterministic request ID,
        # which ConversationService resolves to the original Run. An "unknown"
        # row is reconciled the same way instead of staying rejected forever.
        async with runtime.inbound_lock:
            claim = self.store.claim_inbound(event)
            if not claim.created:
                if claim.event_digest != inbound_digest(event):
                    raise ChannelInboundConflict(
                        "external_message_id was reused for different content"
                    )
                if claim.state in {"accepted", "rejected"}:
                    return ChannelInboundResult(
                        state=claim.state,
                        duplicate=True,
                        conversation_id=claim.conversation_id,
                        run_id=claim.run_id,
                        error=claim.error,
                    )
            request_id = self._inbound_request_id(event)
            try:
                conversation = self.conversations.ensure_channel_conversation(
                    event.platform.value,
                    event.connection_id,
                    event.sender_id,
                    event.conversation_binding,
                    event.text,
                )
                run = self.conversations.submit(
                    conversation.conversation_id,
                    event.text,
                    ConversationSource.CHANNEL,
                    request_id,
                )
            except asyncio.CancelledError:
                recovered = self._reconcile_inbound(event, request_id)
                if recovered is not None:
                    self.store.complete_inbound(
                        event.connection_id,
                        event.external_message_id,
                        state="accepted",
                        conversation_id=recovered[0],
                        run_id=recovered[1],
                    )
                else:
                    self.store.complete_inbound(
                        event.connection_id,
                        event.external_message_id,
                        state="unknown",
                        error="cancelled before admission completed",
                    )
                raise
            except Exception as exc:
                error = self._safe_error(exc, runtime)
                recovered = self._reconcile_inbound(event, request_id)
                if recovered is not None:
                    self.store.complete_inbound(
                        event.connection_id,
                        event.external_message_id,
                        state="accepted",
                        conversation_id=recovered[0],
                        run_id=recovered[1],
                    )
                    return ChannelInboundResult(
                        state="accepted",
                        conversation_id=recovered[0],
                        run_id=recovered[1],
                    )
                if isinstance(exc, ValueError):
                    # Validation and admission-state errors happen before a Run
                    # exists, so this is a definite rejection.
                    self.store.complete_inbound(
                        event.connection_id,
                        event.external_message_id,
                        state="rejected",
                        error=error,
                    )
                    raise ChannelInboundRejected(error) from exc
                self.store.complete_inbound(
                    event.connection_id,
                    event.external_message_id,
                    state="unknown",
                    error=error,
                )
                raise ChannelInboundUncertain(error) from exc
            self.store.complete_inbound(
                event.connection_id,
                event.external_message_id,
                state="accepted",
                conversation_id=conversation.conversation_id,
                run_id=run.run_id,
            )
            return ChannelInboundResult(
                state="accepted",
                conversation_id=conversation.conversation_id,
                run_id=run.run_id,
            )

    @staticmethod
    def _sender_authorized(connection: ChannelConnection, sender_id: str) -> bool:
        if (
            connection.owner_identity is not None
            and sender_id == connection.owner_identity
        ):
            return True
        return sender_id in connection.allowed_senders

    def _adapter_running(self, connection: ChannelConnection) -> bool:
        if connection.desired_state is not ChannelDesiredState.RUNNING:
            return False
        runtime = self._runtime(connection.connection_id)
        adapter = runtime.adapter
        if adapter is None and connection.legacy:
            adapter = self._legacy
        return (
            adapter is not None
            and adapter.health().actual_state is ChannelActualState.RUNNING
        )

    def _reconcile_inbound(
        self, event: ChannelInboundEvent, request_id: str
    ) -> tuple[str, str] | None:
        """Use the deterministic request ID to find work an attempt may have made."""
        try:
            conversation = self.conversations.get_channel_conversation(
                event.platform.value,
                event.connection_id,
                event.sender_id,
                event.conversation_binding,
            )
            if conversation is None:
                return None
            run = self.conversations.run_by_request(
                conversation.conversation_id, request_id
            )
        except Exception:
            return None
        if run is None:
            return None
        return conversation.conversation_id, run.run_id

    async def wait_for_state(
        self,
        connection_id: str,
        state: ChannelActualState,
        *,
        timeout: float = 5.0,
    ) -> ChannelRuntimeStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = self.get_runtime_status(connection_id)
            if status.actual_state is state:
                return status
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"channel {connection_id} did not reach {state.value}"
                )
            await asyncio.sleep(0.01)

    def _require_stored(self, connection_id: str) -> ChannelConnection:
        if self._legacy is not None and connection_id == self._legacy.connection_id:
            raise ChannelLegacyManaged("legacy connections are host-managed")
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ChannelNotFound(connection_id)
        return connection

    def _view_for_inbound(self, connection_id: str) -> ChannelConnection:
        if self._legacy is not None and connection_id == self._legacy.connection_id:
            legacy = self._legacy_view()
            if legacy is not None:
                return legacy.connection
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ChannelNotFound(connection_id)
        return connection

    def _adapter_for_view(self, view: ChannelConnectionView) -> ChannelAdapter:
        if view.connection.legacy:
            if self._legacy is None:
                raise ChannelAdapterError("legacy adapter is not attached")
            return self._legacy
        runtime = self._runtime(view.connection.connection_id)
        if runtime.adapter is None:
            runtime.adapter = self._adapter_for(view.connection)
        return runtime.adapter

    def _adapter_for(self, connection: ChannelConnection) -> ChannelAdapter:
        runtime = self._runtime(connection.connection_id)
        if runtime.adapter is None:
            credentials = self.store.load_credentials(connection.credential_ref)
            if connection.credential_ref is not None and credentials is None:
                raise ChannelAdapterError("channel credentials are unavailable")
            if credentials:
                runtime.secrets = tuple(
                    dict.fromkeys((*runtime.secrets, *credentials.values()))
                )
                if self._secret_sink is not None:
                    self._secret_sink(runtime.secrets)
            runtime.adapter = self._adapters.build(
                connection, credentials, self.accept_inbound
            )
        return runtime.adapter

    def _view_payload(self, view: ChannelConnectionView) -> dict[str, object]:
        return {
            "connection": view.connection.model_dump(mode="json"),
            "runtime": view.runtime.model_dump(mode="json"),
        }

    def _view_from_receipt(
        self, claim: RequestClaim, connection_id: str
    ) -> ChannelConnectionView:
        if claim.result:
            try:
                connection = ChannelConnection.model_validate(
                    claim.result["connection"]
                )
                runtime = ChannelRuntimeStatus.model_validate(claim.result["runtime"])
                return ChannelConnectionView(connection=connection, runtime=runtime)
            except (KeyError, ValueError):
                pass
        return self.get_connection_view(connection_id)

    async def _try_start(self, connection: ChannelConnection) -> ChannelConnectionView:
        runtime = self._runtime(connection.connection_id)
        async with runtime.lock:
            current = self.store.get_connection(connection.connection_id)
            if current is None:
                raise ChannelNotFound(connection.connection_id)
            if (
                not current.enabled
                or current.desired_state is not ChannelDesiredState.RUNNING
            ):
                return self._stored_view(current)
            try:
                adapter = self._adapter_for(current)
                health = adapter.health()
                if health.actual_state is ChannelActualState.RUNNING:
                    # A repeated start must not open a second transport or
                    # session for a connection that is already up.
                    runtime.attempts = 0
                    runtime.last_error = None
                    runtime.next_retry_at = None
                    return self._stored_view(current)
                if health.actual_state is ChannelActualState.CONNECTING:
                    # Do not call start again while the adapter is connecting;
                    # count the wait so recovery stays bounded.
                    runtime.attempts += 1
                    runtime.next_retry_at = None
                    return self._stored_view(current)
                await adapter.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.attempts += 1
                runtime.last_error = self._safe_error(exc, runtime)
                runtime.next_retry_at = None
                health = (
                    runtime.adapter.health()
                    if runtime.adapter is not None
                    else ChannelAdapterHealth(
                        actual_state=ChannelActualState.ERROR,
                        last_error=runtime.last_error,
                    )
                )
                return ChannelConnectionView(
                    connection=current,
                    runtime=ChannelRuntimeStatus(
                        connection_id=current.connection_id,
                        # A failed start is an error even if the adapter has not
                        # updated its own health yet.
                        actual_state=ChannelActualState.ERROR,
                        last_connected_at=health.last_connected_at,
                        last_heartbeat_at=health.last_heartbeat_at,
                        last_error=runtime.last_error,
                    ),
                )
            runtime.attempts = 0
            runtime.last_error = None
            runtime.next_retry_at = None
            return self._stored_view(current)

    def _is_stopping(self) -> bool:
        return self._stopping

    def _schedule_retry(self, connection_id: str, *, immediate: bool = False) -> None:
        if self._is_stopping():
            return
        runtime = self._runtime(connection_id)
        connection = self.store.get_connection(connection_id)
        if connection is None:
            return
        if not self._adapters.supports(connection.platform):
            runtime.last_error = _not_implemented(connection.platform)
            runtime.next_retry_at = None
            return
        if runtime.retry_task is not None and not runtime.retry_task.done():
            return
        runtime.retry_task = asyncio.create_task(
            self._retry_loop(connection_id, immediate=immediate)
        )

    async def _retry_loop(self, connection_id: str, *, immediate: bool) -> None:
        runtime = self._runtime(connection_id)
        try:
            while not self._is_stopping():
                connection = self.store.get_connection(connection_id)
                if (
                    connection is None
                    or not connection.enabled
                    or connection.desired_state is not ChannelDesiredState.RUNNING
                ):
                    return
                if not immediate:
                    delay = self._retry_delays[
                        min(max(runtime.attempts - 1, 0), len(self._retry_delays) - 1)
                    ]
                    runtime.next_retry_at = _future(delay)
                    await self._sleep(delay)
                    if self._is_stopping():
                        return
                immediate = False
                runtime.next_retry_at = None
                connection = self.store.get_connection(connection_id)
                if (
                    connection is None
                    or not connection.enabled
                    or connection.desired_state is not ChannelDesiredState.RUNNING
                ):
                    return
                view = await self._try_start(connection)
                if view.runtime.actual_state is ChannelActualState.RUNNING:
                    runtime.next_retry_at = None
                    return
                if runtime.attempts >= len(self._retry_delays):
                    runtime.next_retry_at = None
                    return
        finally:
            if runtime.retry_task is asyncio.current_task():
                runtime.retry_task = None

    async def _cancel_retry(self, connection_id: str) -> None:
        runtime = self._runtime(connection_id)
        task = runtime.retry_task
        if task is None or task is asyncio.current_task():
            return
        runtime.retry_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _stop_locked(self, runtime: _Runtime) -> None:
        async with runtime.lock:
            await self._stop_adapter(runtime)

    async def _stop_adapter(self, runtime: _Runtime) -> None:
        if runtime.adapter is not None and not runtime.adapter.externally_managed:
            await runtime.adapter.stop()

    def _drop_adapter(self, connection_id: str) -> None:
        runtime = self._runtime(connection_id)
        runtime.adapter = None
        runtime.attempts = 0
        runtime.next_retry_at = None

    @staticmethod
    def _inbound_request_id(event: ChannelInboundEvent) -> str:
        raw = f"channel-inbound:{event.connection_id}:{event.external_message_id}"
        return "channel:" + hashlib.sha256(raw.encode()).hexdigest()[:48]

    @staticmethod
    def _safe_error(exc: BaseException, runtime: _Runtime) -> str:
        # Redact before truncating so a cut cannot leave a secret prefix.
        return redact_text(f"{type(exc).__name__}: {exc}", runtime.secrets)[:400]


__all__ = ["ChannelConnectionView", "ChannelIntake", "ChannelManager"]
