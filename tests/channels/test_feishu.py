"""Feishu adapter protocol tests against an injected SDK channel boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cairn.channels import feishu
from cairn.channels.adapter import ChannelAdapterRegistry
from cairn.channels.feishu import (
    FeishuAdapter,
    feishu_adapter_factory,
    split_message,
)
from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelConnection,
    ChannelCredentialError,
    ChannelDesiredState,
    ChannelInboundEvent,
    ChannelInboundUncertain,
    ChannelPlatform,
    ChannelReplyRejected,
    ChannelSendStatus,
)
from cairn.channels.store import ChannelStore
from tests.channels.support import FakeIntake

APP_ID = "cli_aaaaaaaaaaaaaaaa"
APP_ID_2 = "cli_bbbbbbbbbbbbbbbb"
APP_SECRET = "feishu-secret-value-123"
APP_SECRET_2 = "feishu-secret-value-456"
BOT_OPEN_ID = "ou_bot_111111111111"
BOT_OPEN_ID_2 = "ou_bot_222222222222"
OWNER = "ou_owner_111111111"
ALLOWED = "ou_allowed_1111111"


@dataclass
class FakeIdentity:
    open_id: str


@dataclass
class FakeError:
    code: str
    hint: str = ""
    retry_after_seconds: float | None = None


@dataclass
class FakeSendResult:
    success: bool
    error: FakeError | None = None


class FakeFeishuChannel:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        open_id: str = BOT_OPEN_ID,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.bot_identity: FakeIdentity | None = FakeIdentity(open_id)
        self.handlers: dict[str, list[Any]] = {}
        self.started = False
        self.stopped = False
        self.start_error: BaseException | None = None
        self.send_script: list[FakeSendResult | BaseException] = []
        self.sent: list[tuple[str, str, Any]] = []

    @property
    def is_ready(self) -> bool:
        return self.started and not self.stopped

    async def start_background(self, *, timeout: float | None = 30.0) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def stop_background(self) -> None:
        self.stopped = True

    async def resolve_bot_identity(self) -> FakeIdentity | None:
        return self.bot_identity

    def on(self, name: str, handler: Any) -> Any:
        self.handlers.setdefault(name, []).append(handler)

        def unsubscribe() -> None:
            self.handlers[name].remove(handler)

        return unsubscribe

    async def send(
        self, to: object, message: object, opts: object | None = None
    ) -> Any:
        self.sent.append((str(to), str(message), opts))
        action = self.send_script.pop(0) if self.send_script else None
        if isinstance(action, BaseException):
            raise action
        return action or FakeSendResult(success=True)

    async def emit(self, inbound: object) -> None:
        for handler in list(self.handlers.get("message", [])):
            await handler(inbound)

    def emit_reconnecting(self) -> None:
        for handler in list(self.handlers.get("reconnecting", [])):
            handler()

    def emit_reconnected(self) -> None:
        for handler in list(self.handlers.get("reconnected", [])):
            handler()


class ChannelFactory:
    def __init__(self, *channels: FakeFeishuChannel) -> None:
        self.channels = list(channels)
        self.created: list[FakeFeishuChannel] = []

    def __call__(self, app_id: str, app_secret: str) -> FakeFeishuChannel:
        if self.channels:
            channel = self.channels.pop(0)
            channel.app_id = app_id
            channel.app_secret = app_secret
        else:
            channel = FakeFeishuChannel(app_id, app_secret)
        self.created.append(channel)
        return channel


def inbound(
    message_id: str = "om_1",
    text: str = "hello",
    *,
    sender_id: str = OWNER,
    chat_id: str = "oc_chat_1",
    chat_type: str = "p2p",
    is_bot: bool = False,
    content: object | None = None,
) -> Any:
    return SimpleNamespace(
        id=message_id,
        conversation=SimpleNamespace(chat_id=chat_id, chat_type=chat_type),
        sender=SimpleNamespace(open_id=sender_id, is_bot=is_bot),
        content=content if content is not None else SimpleNamespace(text=text),
    )


def connection(connection_id: str = "ch-feishu") -> ChannelConnection:
    return ChannelConnection(
        connection_id=connection_id,
        platform=ChannelPlatform.FEISHU,
        display_name="Feishu",
        enabled=True,
        desired_state=ChannelDesiredState.RUNNING,
        owner_identity=OWNER,
    )


def adapter_with(
    channel: FakeFeishuChannel,
    *,
    connection_id: str = "ch-feishu",
    credentials: dict[str, str] | None = None,
    sink: Any = None,
) -> FeishuAdapter:
    async def ignore(_: ChannelInboundEvent) -> None:
        return None

    return FeishuAdapter(
        connection(connection_id),
        credentials
        if credentials is not None
        else {"app_id": APP_ID, "app_secret": APP_SECRET},
        sink or ignore,
        channel_factory=lambda app_id, app_secret: channel,
        sleep=no_sleep,
    )


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def ignore(_: ChannelInboundEvent) -> None:
    return None


async def wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def clear_feishu_reservations() -> Any:
    feishu._ACTIVE.app_connections.clear()
    feishu._ACTIVE.ws_connection = None
    yield
    feishu._ACTIVE.app_connections.clear()
    feishu._ACTIVE.ws_connection = None


def test_split_message_prefers_newlines_and_keeps_the_limit() -> None:
    text = ("a" * 4000) + "\n" + ("b" * 4000)
    chunks = split_message(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= 3500 for chunk in chunks)
    assert chunks[0].endswith("a")
    assert chunks[1].startswith("a")


def test_missing_and_malformed_credentials_never_reach_the_sdk(tmp_path: Path) -> None:
    async def scenario() -> None:
        for credentials, expected in (
            (None, "feishu_app_id_missing"),
            ({"app_id": "bad id", "app_secret": APP_SECRET}, "feishu_app_id_invalid"),
            ({"app_id": APP_ID}, "feishu_app_secret_missing"),
        ):
            adapter = FeishuAdapter(
                connection(),
                credentials,
                ignore,
                channel_factory=lambda app_id, secret: pytest.fail(
                    "the SDK must not be reached"
                ),
            )
            with pytest.raises(ChannelCredentialError) as excinfo:
                await adapter.start()
            assert str(excinfo.value) == expected
            assert adapter.health().actual_state is ChannelActualState.ERROR

    asyncio.run(scenario())


def test_start_resolves_a_trusted_identity_and_same_app_cannot_compete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        first = adapter_with(first_channel)
        await first.start()
        assert first.health().actual_state is ChannelActualState.RUNNING
        assert first.identity() == f"feishu:{APP_ID}:{BOT_OPEN_ID}"
        assert first_channel.started is True

        second = adapter_with(
            FakeFeishuChannel(APP_ID, APP_SECRET),
            connection_id="ch-second",
        )
        with pytest.raises(ChannelCredentialError) as excinfo:
            await second.start()
        assert str(excinfo.value) == "feishu_app_in_use"
        assert second.health().actual_state is ChannelActualState.ERROR

        # lark-oapi's WS client owns a process-global loop; a second app
        # cannot run in the same process even with different credentials.
        third = adapter_with(
            FakeFeishuChannel(APP_ID_2, APP_SECRET_2),
            connection_id="ch-third",
            credentials={"app_id": APP_ID_2, "app_secret": APP_SECRET_2},
        )
        with pytest.raises(ChannelCredentialError) as excinfo:
            await third.start()
        assert str(excinfo.value) == "feishu_ws_busy"

        await first.stop()
        await third.start()
        assert third.health().actual_state is ChannelActualState.RUNNING
        await third.stop()

    asyncio.run(scenario())


def test_start_failure_redacts_the_app_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        channel.start_error = RuntimeError(f"handshake rejected {APP_SECRET}")
        adapter = adapter_with(channel)
        with pytest.raises(ChannelAdapterError) as excinfo:
            await adapter.start()
        assert APP_SECRET not in str(excinfo.value)
        last_error = adapter.health().last_error or ""
        assert APP_SECRET not in last_error
        assert "<redacted>" in last_error
        assert adapter.health().actual_state is ChannelActualState.ERROR

    asyncio.run(scenario())


def test_unresolved_identity_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        channel.bot_identity = None
        adapter = adapter_with(channel)
        with pytest.raises(ChannelAdapterError) as excinfo:
            await adapter.start()
        assert "feishu_identity_unavailable" in str(excinfo.value)
        assert adapter.identity() is None
        assert adapter.health().actual_state is ChannelActualState.ERROR

    asyncio.run(scenario())


def test_only_private_human_text_reaches_the_inbound_sink(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        received: list[ChannelInboundEvent] = []

        async def sink(event: ChannelInboundEvent) -> None:
            received.append(event)

        adapter = adapter_with(channel, sink=sink)
        await adapter.start()
        await channel.emit(inbound())
        await channel.emit(inbound(message_id="om_group", chat_type="group"))
        await channel.emit(inbound(message_id="om_bot", sender_id=BOT_OPEN_ID))
        await channel.emit(inbound(message_id="om_bot_flag", is_bot=True))
        await channel.emit(
            inbound(message_id="om_image", content=SimpleNamespace(image_key="x"))
        )
        await channel.emit(inbound(message_id="om_blank", text="   "))
        assert [event.external_message_id for event in received] == ["om_1"]
        event = received[0]
        assert event.sender_id == OWNER
        assert event.conversation_binding == "oc_chat_1"
        assert event.adapter_identity == f"feishu:{APP_ID}:{BOT_OPEN_ID}"
        await adapter.stop()

    asyncio.run(scenario())


def test_reconnect_callbacks_make_the_transport_state_observable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()
        channel.emit_reconnecting()
        health = adapter.health()
        assert health.actual_state is ChannelActualState.CONNECTING
        assert health.last_error == "feishu_reconnecting"
        channel.emit_reconnected()
        health = adapter.health()
        assert health.actual_state is ChannelActualState.RUNNING
        assert health.last_error is None
        assert health.last_heartbeat_at is not None
        await adapter.stop()
        assert adapter.health().actual_state is ChannelActualState.STOPPED
        assert adapter.identity() is None

    asyncio.run(scenario())


def test_inbound_during_reconnect_is_held_not_dropped(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        received: list[ChannelInboundEvent] = []

        async def sink(event: ChannelInboundEvent) -> None:
            received.append(event)

        adapter = adapter_with(channel, sink=sink)
        await adapter.start()
        channel.emit_reconnecting()
        await channel.emit(inbound(message_id="om_reconnecting"))
        assert received == []
        channel.emit_reconnected()
        await wait_until(lambda: len(received) == 1)
        assert received[0].external_message_id == "om_reconnecting"
        await adapter.stop()

    asyncio.run(scenario())


def test_empty_outbound_text_is_rejected_before_the_sdk(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()
        with pytest.raises(ChannelAdapterError):
            await adapter.send("oc_chat_1", "   ", "reply-empty")
        assert channel.sent == []
        await adapter.stop()

    asyncio.run(scenario())


def test_reconnect_opens_a_fresh_transport(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = ChannelFactory()
        adapter = FeishuAdapter(
            connection(),
            {"app_id": APP_ID, "app_secret": APP_SECRET},
            ignore,
            channel_factory=factory,
            sleep=no_sleep,
        )
        await adapter.start()
        assert len(factory.created) == 1
        await adapter.reconnect()
        assert len(factory.created) == 2
        assert factory.created[0].stopped is True
        assert factory.created[1].started is True
        assert adapter.health().actual_state is ChannelActualState.RUNNING
        await adapter.stop()

    asyncio.run(scenario())


def test_send_classifies_definite_refusals_and_unknown_outcomes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()

        channel.send_script = [
            FakeSendResult(
                success=False,
                error=FakeError(code="permission_denied", hint="no im:message"),
            )
        ]
        result = await adapter.send("oc_chat_1", "hello", "reply-1")
        assert result.status is ChannelSendStatus.REJECTED
        assert result.error is not None and "permission_denied" in result.error

        channel.send_script = [
            FakeSendResult(success=False, error=FakeError(code="send_timeout"))
        ]
        result = await adapter.send("oc_chat_1", "hello", "reply-2")
        assert result.status is ChannelSendStatus.UNKNOWN

        channel.send_script = [
            FakeSendResult(
                success=False,
                error=FakeError(code="rate_limited", retry_after_seconds=2),
            ),
            FakeSendResult(success=True),
        ]
        waits: list[float] = []

        async def record_sleep(seconds: float) -> None:
            waits.append(seconds)

        adapter._sleep = record_sleep
        result = await adapter.send("oc_chat_1", "hello", "reply-3")
        assert result.status is ChannelSendStatus.ACCEPTED
        assert waits == [2.0]
        assert len(channel.sent) == 4
        await adapter.stop()

    asyncio.run(scenario())


def test_partial_send_is_unknown_and_never_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()
        channel.send_script = [
            FakeSendResult(success=True),
            FakeSendResult(success=False, error=FakeError(code="send_timeout")),
        ]
        result = await adapter.send("oc_chat_1", "a" * 4000, "reply-long")
        assert result.status is ChannelSendStatus.UNKNOWN
        assert result.error is not None
        assert result.error.startswith("feishu_partial_send")
        await adapter.stop()

    asyncio.run(scenario())


def test_send_error_hints_never_echo_the_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()
        channel.send_script = [
            FakeSendResult(
                success=False,
                error=FakeError(code="unknown", hint=f"token={APP_SECRET}"),
            )
        ]
        result = await adapter.send("oc_chat_1", "hello", "reply-secret")
        assert result.status is ChannelSendStatus.UNKNOWN
        assert result.error is not None
        assert APP_SECRET not in result.error
        assert "<redacted>" in result.error
        await adapter.stop()

    asyncio.run(scenario())


def test_unexpected_send_exception_is_outcome_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        adapter = adapter_with(channel)
        await adapter.start()
        channel.send_script = [RuntimeError(f"timeout with {APP_SECRET}")]
        result = await adapter.send("oc_chat_1", "hello", "reply-exc")
        assert result.status is ChannelSendStatus.UNKNOWN
        assert result.error is not None
        assert APP_SECRET not in result.error
        await adapter.stop()

    asyncio.run(scenario())


def test_uncertain_admission_is_retried_without_blocking_the_callback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        attempts = 0
        accepted: list[ChannelInboundEvent] = []

        async def sink(event: ChannelInboundEvent) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ChannelInboundUncertain("conversation is busy")
            accepted.append(event)

        adapter = adapter_with(channel, sink=sink)
        await adapter.start()
        await channel.emit(inbound())
        assert accepted == []
        await wait_until(lambda: len(accepted) == 1)
        assert attempts == 3
        assert adapter.health().last_error is None
        await adapter.stop()

    asyncio.run(scenario())


async def manager_for(
    tmp_path: Path,
    factory: ChannelFactory,
    intake: FakeIntake,
) -> tuple[ChannelManager, ChannelStore, str]:
    store = ChannelStore(tmp_path / "channels")
    registry = ChannelAdapterRegistry().register(
        ChannelPlatform.FEISHU,
        feishu_adapter_factory(channel_factory=factory, sleep=no_sleep),
    )
    manager = ChannelManager(
        store=store,
        conversations=intake,
        adapters=registry,
        retry_delays=(0,),
    )
    created = await manager.create_connection(
        platform=ChannelPlatform.FEISHU,
        display_name="Feishu",
        enabled=True,
        owner_identity=OWNER,
        metadata={},
        credentials={"app_id": APP_ID, "app_secret": APP_SECRET},
        request_id="create-feishu",
        allowed_senders=(ALLOWED,),
    )
    connection_id = created.connection.connection_id
    await manager.start_connection(connection_id, "start-feishu")
    return manager, store, connection_id


def test_manager_admits_owner_once_and_rejects_other_senders(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        factory = ChannelFactory(channel)
        intake = FakeIntake()
        manager, _, connection_id = await manager_for(tmp_path, factory, intake)

        await channel.emit(inbound(text="介绍一下 Cairn"))
        await channel.emit(inbound(text="介绍一下 Cairn"))  # transport redelivery
        await channel.emit(inbound(message_id="om_allowed", sender_id=ALLOWED))
        await channel.emit(inbound(message_id="om_unknown", sender_id="ou_stranger"))
        assert len(intake.submissions) == 2
        assert intake.submissions[0][1] == "介绍一下 Cairn"
        assert intake.submissions[1][1] == "hello"
        metadata = intake.metadata[0]
        assert metadata is not None
        principal = metadata["channel_principal"]
        assert isinstance(principal, dict)
        assert principal["role"] == "owner"
        assert principal["sender_id"] == OWNER
        assert principal["connection_id"] == connection_id
        guest = intake.metadata[1]
        assert guest is not None
        guest_principal = guest["channel_principal"]
        assert isinstance(guest_principal, dict)
        assert guest_principal["role"] == "allowed"

        await manager.stop_connection(connection_id, "stop-feishu")
        await manager.close()

    asyncio.run(scenario())


def test_admission_response_loss_reconciles_and_redelivery_is_idempotent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        intake = FakeIntake(fail_after_submit=True)
        manager, _, connection_id = await manager_for(
            tmp_path, ChannelFactory(channel), intake
        )
        await channel.emit(inbound(text="first"))
        assert len(intake.submissions) == 1
        await channel.emit(inbound(text="first"))
        assert len(intake.submissions) == 1
        await manager.stop_connection(connection_id, "stop-feishu")
        await manager.close()

    asyncio.run(scenario())


def test_restart_keeps_the_same_run_for_a_redelivered_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        intake = FakeIntake()
        first, store, connection_id = await manager_for(
            tmp_path, ChannelFactory(first_channel), intake
        )
        await first_channel.emit(inbound(text="remember me"))
        assert len(intake.submissions) == 1
        run_id = next(iter(intake.runs.values())).run_id
        await first.close()

        second_channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        registry = ChannelAdapterRegistry().register(
            ChannelPlatform.FEISHU,
            feishu_adapter_factory(
                channel_factory=ChannelFactory(second_channel), sleep=no_sleep
            ),
        )
        second = ChannelManager(
            store=store,
            conversations=intake,
            adapters=registry,
            retry_delays=(0,),
        )
        await second.start()
        await wait_until(
            lambda: (
                second.get_connection_view(connection_id).runtime.actual_state
                is ChannelActualState.RUNNING
            )
        )
        await second_channel.emit(inbound(text="remember me"))
        assert len(intake.submissions) == 1
        claim = store.reply_for_run(run_id)
        assert claim is not None
        await second.stop_connection(connection_id, "stop-second")
        await second.close()

    asyncio.run(scenario())


def test_outbound_reply_is_identity_bound_and_old_app_reply_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        second_channel = FakeFeishuChannel(
            APP_ID_2, APP_SECRET_2, open_id=BOT_OPEN_ID_2
        )
        factory = ChannelFactory(first_channel, second_channel)
        intake = FakeIntake()
        manager, store, connection_id = await manager_for(tmp_path, factory, intake)
        await first_channel.emit(inbound(text="first app"))
        reply = store.reply_for_run("run-1")
        assert reply is not None

        result = await manager.send_reply(reply, "answer", "channel-reply:run-1")
        assert result.status is ChannelSendStatus.ACCEPTED
        assert first_channel.sent[0][0] == "oc_chat_1"
        assert first_channel.sent[0][1] == "answer"

        # Replacing the application identity opens a new dedupe namespace...
        await manager.update_connection(
            connection_id,
            expected_revision=1,
            request_id="replace-app",
            changes={},
            credentials={"app_id": APP_ID_2, "app_secret": APP_SECRET_2},
            replace_credentials=True,
        )
        assert factory.created[-1] is second_channel
        assert second_channel.app_id == APP_ID_2
        await wait_until(
            lambda: (
                manager.get_connection_view(connection_id).runtime.actual_state
                is ChannelActualState.RUNNING
            )
        )
        await second_channel.emit(inbound(message_id="om_1", text="second app"))
        assert len(intake.submissions) == 2

        # ...but a pending reply captured under the old App is not delivered by
        # the new one.
        with pytest.raises(ChannelReplyRejected) as excinfo:
            await manager.send_reply(reply, "stale answer", "channel-reply:run-1")
        assert str(excinfo.value) == "reply_bot_mismatch"
        assert all(item[1] != "stale answer" for item in second_channel.sent)

        await manager.stop_connection(connection_id, "stop-feishu")
        await manager.close()

    asyncio.run(scenario())


def test_revoked_sender_blocks_a_pending_reply(tmp_path: Path) -> None:
    async def scenario() -> None:
        channel = FakeFeishuChannel(APP_ID, APP_SECRET)
        intake = FakeIntake()
        manager, store, connection_id = await manager_for(
            tmp_path, ChannelFactory(channel), intake
        )
        await channel.emit(inbound(text="owner request"))
        reply = store.reply_for_run("run-1")
        assert reply is not None
        await manager.update_connection(
            connection_id,
            expected_revision=1,
            request_id="revoke-owner",
            changes={"owner_identity": "ou_someone_else"},
        )
        with pytest.raises(ChannelReplyRejected) as excinfo:
            await manager.send_reply(reply, "answer", "channel-reply:run-1")
        assert str(excinfo.value) == "sender_not_authorized"
        assert channel.sent == []
        await manager.stop_connection(connection_id, "stop-feishu")
        await manager.close()

    asyncio.run(scenario())
