"""Connection isolation, recovery, inbound dedupe and outbound idempotency."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cairn.channels.manager import ChannelConnectionView, ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelConfirmationRequired,
    ChannelDeliveryUncertain,
    ChannelDesiredState,
    ChannelDisabled,
    ChannelDisplayState,
    ChannelInboundConflict,
    ChannelInboundRejected,
    ChannelNotFound,
    ChannelPlatform,
    ChannelPlatformSummary,
    ChannelRequestConflict,
    ChannelSendStatus,
)
from cairn.conversation.models import ConversationSource
from tests.channels.support import (
    CapturingFactory,
    inbound,
    make_manager,
)

PLATFORMS = {
    ChannelPlatform.WECHAT,
    ChannelPlatform.QQ,
    ChannelPlatform.TELEGRAM,
    ChannelPlatform.DISCORD,
}


async def _create(
    manager: ChannelManager,
    platform: ChannelPlatform = ChannelPlatform.TELEGRAM,
    **kwargs: object,
) -> ChannelConnectionView:
    values: dict[str, object] = {
        "display_name": "Test",
        "enabled": True,
        "owner_identity": None,
        "metadata": {},
        "credentials": None,
        "request_id": "create-1",
    }
    values.update(kwargs)
    return await manager.create_connection(platform=platform, **values)  # type: ignore[arg-type]


def _summary(
    summaries: tuple[ChannelPlatformSummary, ...], platform: ChannelPlatform
) -> ChannelPlatformSummary:
    return next(item for item in summaries if item.platform is platform)


def test_zero_config_lists_every_platform_without_a_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, _, _, _ = make_manager(tmp_path / "channels")
        summaries = manager.platform_summaries()
        assert {item.platform for item in summaries} == PLATFORMS
        assert all(
            item.display_state is ChannelDisplayState.UNCONFIGURED for item in summaries
        )
        assert manager.list_connections() == ()
        await manager.close()

    asyncio.run(scenario())


def test_multiple_connections_start_stop_and_reconnect_independently(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, factory = make_manager(tmp_path / "channels")
        telegram = await _create(
            manager, ChannelPlatform.TELEGRAM, display_name="Telegram", request_id="t"
        )
        discord = await _create(
            manager, ChannelPlatform.DISCORD, display_name="Discord", request_id="d"
        )
        assert len(manager.list_connections()) == 2
        summary = _summary(manager.platform_summaries(), ChannelPlatform.TELEGRAM)
        assert summary.display_state is ChannelDisplayState.SILENT
        assert summary.connections == 1
        assert summary.running == 0

        started = await manager.start_connection(
            telegram.connection.connection_id, "start-t"
        )
        assert started.runtime.actual_state is ChannelActualState.RUNNING
        repeated = await manager.start_connection(
            telegram.connection.connection_id, "start-t-again"
        )
        assert repeated.runtime.actual_state is ChannelActualState.RUNNING
        assert factory.adapters[telegram.connection.connection_id].starts == 1
        assert (
            manager.get_connection_view(
                discord.connection.connection_id
            ).runtime.actual_state
            is ChannelActualState.STOPPED
        )
        assert (
            _summary(
                manager.platform_summaries(), ChannelPlatform.TELEGRAM
            ).display_state
            is ChannelDisplayState.RUNNING
        )

        stopped = await manager.stop_connection(
            telegram.connection.connection_id, "stop-t"
        )
        assert stopped.runtime.actual_state is ChannelActualState.STOPPED
        assert stopped.connection.desired_state is ChannelDesiredState.STOPPED

        reconnected = await manager.reconnect_connection(
            discord.connection.connection_id, "reconnect-d"
        )
        assert reconnected.runtime.actual_state is ChannelActualState.RUNNING
        adapter = factory.adapters[discord.connection.connection_id]
        assert adapter.reconnects == 1
        await manager.close()

    asyncio.run(scenario())


def test_a_failing_connection_does_not_affect_another_and_retries_are_bounded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, factory = make_manager(
            tmp_path / "channels", retry_delays=(0, 0, 0)
        )
        broken = await _create(
            manager, ChannelPlatform.QQ, display_name="Broken", request_id="broken"
        )
        healthy = await _create(
            manager, ChannelPlatform.DISCORD, display_name="Healthy", request_id="ok"
        )
        factory.fail_starts.add(broken.connection.connection_id)

        failed = await manager.start_connection(
            broken.connection.connection_id, "start-broken"
        )
        assert failed.runtime.actual_state is ChannelActualState.ERROR
        assert failed.runtime.last_error is not None
        running = await manager.start_connection(
            healthy.connection.connection_id, "start-ok"
        )
        assert running.runtime.actual_state is ChannelActualState.RUNNING

        runtime = manager._runtime(broken.connection.connection_id)
        for _ in range(1000):
            if runtime.retry_task is None:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("retry loop did not stop")
        adapter = factory.adapters[broken.connection.connection_id]
        assert adapter.starts == 3
        assert (
            manager.get_connection_view(
                healthy.connection.connection_id
            ).runtime.actual_state
            is ChannelActualState.RUNNING
        )
        await manager.close()

    asyncio.run(scenario())


def test_restart_restores_only_desired_running_connections(tmp_path: Path) -> None:
    root = tmp_path / "channels"

    async def first() -> str:
        manager, _, _, _ = make_manager(root)
        connection = await _create(manager, request_id="restore-create")
        stopped = await _create(
            manager, ChannelPlatform.DISCORD, request_id="restore-stopped"
        )
        await manager.start_connection(
            connection.connection.connection_id, "restore-start"
        )
        await manager.start_connection(
            stopped.connection.connection_id, "restore-stop-start"
        )
        await manager.stop_connection(stopped.connection.connection_id, "restore-stop")
        await manager.close()
        return connection.connection.connection_id

    async def second(connection_id: str) -> None:
        factory = CapturingFactory()
        manager, _, _, _ = make_manager(root, factory=factory)
        await manager.start()
        status = await manager.wait_for_state(connection_id, ChannelActualState.RUNNING)
        assert status.actual_state is ChannelActualState.RUNNING
        assert factory.adapters[connection_id].starts == 1
        stopped = [
            connection
            for connection in manager.list_connections()
            if connection.connection_id != connection_id
        ]
        assert len(stopped) == 1
        assert stopped[0].desired_state is ChannelDesiredState.STOPPED
        assert stopped[0].connection_id not in factory.adapters
        await manager.close()

    connection_id = asyncio.run(first())
    asyncio.run(second(connection_id))


def test_inbound_dedupe_submits_once_and_rejects_changed_reuse(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, intake, _ = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="inbound-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "inbound-start")

        first = await manager.accept_inbound(inbound(connection_id, "m-1"))
        assert first.state == "accepted"
        assert first.duplicate is False
        assert first.run_id == "run-1"
        assert len(intake.submissions) == 1
        submitted = intake.submissions[0]
        assert submitted[0].startswith(f"conv-{connection_id}")
        assert submitted[2] is ConversationSource.CHANNEL

        duplicate = await manager.accept_inbound(inbound(connection_id, "m-1"))
        assert duplicate.duplicate is True
        assert duplicate.run_id == "run-1"
        assert len(intake.submissions) == 1

        with pytest.raises(ChannelInboundConflict):
            await manager.accept_inbound(
                inbound(connection_id, "m-1", text="changed content")
            )

        intake.fail = True
        with pytest.raises(ChannelInboundRejected):
            await manager.accept_inbound(inbound(connection_id, "m-2"))
        rejected = await manager.accept_inbound(inbound(connection_id, "m-2"))
        assert rejected.duplicate is True
        assert rejected.state == "rejected"
        assert len(intake.submissions) == 1
        await manager.close()

    asyncio.run(scenario())


def test_send_replays_accepted_and_never_resends_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, _, _, factory = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="send-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "send-start")
        adapter = factory.adapters[connection_id]

        first = await manager.send(connection_id, "chat", "hello", "key-1")
        assert first.status is ChannelSendStatus.ACCEPTED
        again = await manager.send(connection_id, "chat", "hello", "key-1")
        assert again.status is ChannelSendStatus.ACCEPTED
        assert adapter.sent == [("chat", "hello", "key-1")]

        adapter.fail_send = True
        uncertain = await manager.send(connection_id, "chat", "second", "key-2")
        assert uncertain.status is ChannelSendStatus.UNKNOWN
        adapter.fail_send = False
        with pytest.raises(ChannelDeliveryUncertain):
            await manager.send(connection_id, "chat", "second", "key-2")
        assert adapter.sent == [("chat", "hello", "key-1")]
        await manager.close()

    asyncio.run(scenario())


def test_credentials_are_registered_for_redaction_and_never_in_views(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        captured: list[tuple[str, ...]] = []
        factory = CapturingFactory()
        factory.raise_with_secret = "top-secret-value"
        manager, _, _, _ = make_manager(
            tmp_path / "channels",
            factory=factory,
            retry_delays=(60,),
            secret_sink=captured.append,
        )
        connection = await _create(
            manager,
            credentials={"token": "top-secret-value"},
            request_id="secret-create",
        )
        connection_id = connection.connection.connection_id
        view = await manager.start_connection(connection_id, "secret-start")
        assert view.runtime.actual_state is ChannelActualState.ERROR
        assert view.runtime.last_error is not None
        assert "top-secret-value" not in view.runtime.last_error
        assert "[redacted]" in view.runtime.last_error
        assert "top-secret-value" not in view.connection.model_dump_json()
        assert ("top-secret-value",) in captured
        await manager.close()

    asyncio.run(scenario())


def test_editing_without_a_secret_keeps_the_adapter_and_replacing_restarts_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, store, _, factory = make_manager(tmp_path / "channels")
        connection = await _create(
            manager, credentials={"token": "old"}, request_id="edit-create"
        )
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "edit-start")
        old_adapter = factory.adapters[connection_id]
        current = manager.get_connection_view(connection_id).connection

        renamed = await manager.update_connection(
            connection_id,
            expected_revision=current.revision,
            request_id="edit-rename",
            changes={"display_name": "Renamed"},
        )
        assert factory.adapters[connection_id] is old_adapter
        assert old_adapter.starts == 1
        assert old_adapter.stops == 0
        assert renamed.connection.credential_ref == current.credential_ref

        replaced = await manager.update_connection(
            connection_id,
            expected_revision=renamed.connection.revision,
            request_id="edit-replace",
            changes={},
            credentials={"token": "new"},
            replace_credentials=True,
        )
        assert old_adapter.stops == 1
        new_adapter = factory.adapters[connection_id]
        assert new_adapter is not old_adapter
        assert new_adapter.starts == 1
        assert replaced.runtime.actual_state is ChannelActualState.RUNNING
        assert factory.credentials[connection_id] == {"token": "new"}
        assert store.load_credentials(replaced.connection.credential_ref) == {
            "token": "new"
        }
        await manager.close()

    asyncio.run(scenario())


def test_delete_requires_confirmation_stops_the_adapter_and_keeps_audit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, store, _, factory = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="delete-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "delete-start")
        await manager.send(connection_id, "chat", "hello", "delete-delivery")
        current = manager.get_connection_view(connection_id).connection
        adapter = factory.adapters[connection_id]

        with pytest.raises(ChannelConfirmationRequired):
            await manager.delete_connection(
                connection_id,
                expected_revision=current.revision,
                request_id="delete-refused",
                confirmed=False,
            )
        assert adapter.stops == 0
        assert store.get_connection(connection_id) is not None

        result = await manager.delete_connection(
            connection_id,
            expected_revision=current.revision,
            request_id="delete-ok",
            confirmed=True,
        )
        assert result.connection_id == connection_id
        assert adapter.stops == 1
        assert store.get_connection(connection_id) is None
        assert [row.status for row in store.list_deliveries(connection_id)] == [
            "accepted"
        ]
        assert connection_id not in manager._runtimes
        with pytest.raises(ChannelNotFound):
            manager.get_connection_view(connection_id)
        await manager.close()

    asyncio.run(scenario())


def test_delete_cancels_a_pending_retry_and_removes_the_runtime(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, store, _, factory = make_manager(
            tmp_path / "channels", retry_delays=(3600,)
        )
        connection = await _create(manager, request_id="retry-delete-create")
        connection_id = connection.connection.connection_id
        factory.fail_starts.add(connection_id)
        failed = await manager.start_connection(connection_id, "retry-delete-start")
        assert failed.runtime.actual_state is ChannelActualState.ERROR
        runtime = manager._runtime(connection_id)
        retry_task = runtime.retry_task
        assert retry_task is not None
        current = manager.get_connection_view(connection_id).connection

        await manager.delete_connection(
            connection_id,
            expected_revision=current.revision,
            request_id="retry-delete",
            confirmed=True,
        )
        assert retry_task.cancelled()
        assert runtime.retry_task is None
        assert manager._runtimes == {}
        assert store.get_connection(connection_id) is None
        assert factory.adapters[connection_id].starts == 1
        await manager.close()

    asyncio.run(scenario())


def test_disabled_connection_refuses_outbound_and_inbound(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, _, _, _ = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="disable-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "disable-start")
        current = manager.get_connection_view(connection_id).connection
        await manager.update_connection(
            connection_id,
            expected_revision=current.revision,
            request_id="disable-update",
            changes={"enabled": False},
        )
        with pytest.raises(ChannelDisabled):
            await manager.send(connection_id, "chat", "hello", "disabled-send")
        with pytest.raises(ChannelInboundRejected):
            await manager.accept_inbound(inbound(connection_id, "disabled-inbound"))
        await manager.close()

    asyncio.run(scenario())


def test_idempotency_key_cannot_be_reused_for_a_different_message(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, _ = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="payload-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "payload-start")
        await manager.send(connection_id, "chat", "first", "same-key")
        with pytest.raises(ChannelRequestConflict):
            await manager.send(connection_id, "chat", "second", "same-key")
        await manager.close()

    asyncio.run(scenario())


def test_restart_marks_inflight_delivery_unknown_and_never_resends(
    tmp_path: Path,
) -> None:
    root = tmp_path / "channels"

    async def first() -> str:
        manager, store, _, _ = make_manager(root)
        connection = await _create(manager, request_id="restart-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "restart-start")
        store.begin_delivery(
            connection_id=connection_id,
            platform=ChannelPlatform.TELEGRAM,
            target="chat",
            message="hello",
            idempotency_key="restart-key",
        )
        await manager.close()
        return connection_id

    async def second(connection_id: str) -> None:
        manager, _, _, factory = make_manager(root)
        await manager.start()
        await manager.wait_for_state(connection_id, ChannelActualState.RUNNING)
        with pytest.raises(ChannelDeliveryUncertain):
            await manager.send(connection_id, "chat", "hello", "restart-key")
        assert factory.adapters[connection_id].sent == []
        await manager.close()

    connection_id = asyncio.run(first())
    asyncio.run(second(connection_id))


def test_claimed_inbound_after_interruption_is_recovered(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager, store, intake, _ = make_manager(tmp_path / "channels")
        connection = await _create(manager, request_id="claimed-create")
        connection_id = connection.connection.connection_id
        await manager.start_connection(connection_id, "claimed-start")
        event = inbound(connection_id, "claimed-message")
        # Simulate a process stopping after the durable claim but before the
        # conversation submission was recorded as accepted.
        assert store.claim_inbound(event).created is True
        recovered = await manager.accept_inbound(event)
        assert recovered.state == "accepted"
        assert recovered.duplicate is False
        assert recovered.run_id == "run-1"
        assert len(intake.submissions) == 1
        duplicate = await manager.accept_inbound(event)
        assert duplicate.duplicate is True
        assert duplicate.run_id == "run-1"
        await manager.close()

    asyncio.run(scenario())
