"""Durable channel configuration, receipts, inbound dedupe and delivery audit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cairn.channels.models import (
    ChannelDesiredState,
    ChannelInboundEvent,
    ChannelPlatform,
    ChannelRequestConflict,
    ChannelRevisionConflict,
    ChannelSendStatus,
    validate_credentials,
    validate_metadata,
)
from cairn.channels.store import ChannelStore, inbound_digest
from cairn.conversation.models import ConversationSource
from cairn.conversation.store import DomainStore


def _store(path: Path) -> ChannelStore:
    return ChannelStore(path / "channels")


def _create(
    store: ChannelStore,
    *,
    request_id: str = "req-1",
    name: str = "Telegram Home",
    credentials: dict[str, str] | None = None,
) -> str:
    result = store.create_connection(
        platform=ChannelPlatform.TELEGRAM,
        display_name=name,
        enabled=True,
        owner_identity="owner",
        metadata={"label": "home"},
        credentials=credentials,
        request_id=request_id,
    )
    return result.connection.connection_id


def _event(
    connection_id: str, message_id: str = "m-1", text: str = "hi"
) -> ChannelInboundEvent:
    return ChannelInboundEvent(
        platform=ChannelPlatform.TELEGRAM,
        connection_id=connection_id,
        external_message_id=message_id,
        sender_id="sender",
        conversation_binding="chat",
        text=text,
    )


def test_store_is_private_and_credentials_are_not_in_connection_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    connection_id = _create(store, credentials={"token": "super-secret"})
    connection = store.get_connection(connection_id)
    assert connection is not None
    assert "super-secret" not in connection.model_dump_json()
    assert connection.credential_ref is not None
    assert store.load_credentials(connection.credential_ref) == {
        "token": "super-secret"
    }


def test_create_replay_and_request_id_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.create_connection(
        platform=ChannelPlatform.TELEGRAM,
        display_name="Home",
        enabled=True,
        owner_identity=None,
        metadata={},
        credentials=None,
        request_id="same",
    )
    replay = store.create_connection(
        platform=ChannelPlatform.TELEGRAM,
        display_name="Home",
        enabled=True,
        owner_identity=None,
        metadata={},
        credentials=None,
        request_id="same",
    )
    assert replay.replayed is True
    assert replay.connection.connection_id == first.connection.connection_id
    with pytest.raises(ChannelRequestConflict):
        store.create_connection(
            platform=ChannelPlatform.TELEGRAM,
            display_name="Different",
            enabled=True,
            owner_identity=None,
            metadata={},
            credentials=None,
            request_id="same",
        )


def test_request_receipt_survives_reopen_and_exposes_pending_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.claim_request(
        "restart-pending",
        operation="update",
        digest_value={"probe": True},
    )
    reopened = _store(tmp_path)
    pending = reopened.request_receipt("restart-pending")
    assert pending is not None
    assert (pending.state, pending.result) == ("pending", None)
    assert reopened.request_receipt("restart-missing") is None

    reopened.complete_request("restart-pending", {"connection": {"revision": 7}})
    completed = _store(tmp_path).request_receipt("restart-pending")
    assert completed is not None
    assert completed.state == "completed"
    assert completed.result == {"connection": {"revision": 7}}


def test_request_receipt_rejects_corrupt_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as database:
        database.executemany(
            "INSERT INTO channel_requests "
            "(request_id, operation, digest, state, result_json, created_at, updated_at) "
            "VALUES (?, 'update', 'digest', 'completed', ?, '', '')",
            [("corrupt-json", "{not-json"), ("corrupt-shape", "[]")],
        )
    with pytest.raises(RuntimeError):
        store.request_receipt("corrupt-json")
    with pytest.raises(RuntimeError):
        store.request_receipt("corrupt-shape")


def test_completed_request_receipts_are_pruned_only_after_retention(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.claim_request("retained", operation="update", digest_value={"probe": 1})
    store.complete_request("retained", {"connection": {"revision": 1}})
    with sqlite3.connect(store.path) as database:
        database.execute(
            "UPDATE channel_requests SET updated_at = ? WHERE request_id = ?",
            ("2000-01-01T00:00:00+00:00", "retained"),
        )
    store.claim_request("fresh", operation="update", digest_value={"probe": 2})
    store.complete_request("fresh", {"connection": {"revision": 2}})
    assert store.request_receipt("retained") is None
    fresh = store.request_receipt("fresh")
    assert fresh is not None
    assert fresh.state == "completed"


def test_update_without_secret_keeps_credentials_and_replacement_is_explicit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store, credentials={"token": "old"})
    current = store.get_connection(connection_id)
    assert current is not None
    old_ref = current.credential_ref

    renamed = store.update_connection(
        connection_id,
        expected_revision=current.revision,
        request_id="rename",
        changes={"display_name": "Renamed"},
    )
    assert renamed.connection.credential_ref == old_ref
    assert store.load_credentials(old_ref) == {"token": "old"}

    replaced = store.update_connection(
        connection_id,
        expected_revision=renamed.connection.revision,
        request_id="replace",
        changes={},
        credentials={"token": "new"},
        replace_credentials=True,
    )
    assert replaced.connection.credential_ref not in {None, old_ref}
    assert store.load_credentials(old_ref) is None
    assert store.load_credentials(replaced.connection.credential_ref) == {
        "token": "new"
    }


def test_revision_conflict_and_disable_forces_stopped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    current = store.get_connection(connection_id)
    assert current is not None
    _ = store.set_desired_state(connection_id, ChannelDesiredState.RUNNING)
    with pytest.raises(ChannelRevisionConflict) as conflict:
        store.update_connection(
            connection_id,
            expected_revision=current.revision + 1,
            request_id="stale",
            changes={"display_name": "Stale"},
        )
    assert conflict.value.current_revision == current.revision
    disabled = store.update_connection(
        connection_id,
        expected_revision=current.revision,
        request_id="disable",
        changes={"enabled": False},
    )
    assert disabled.connection.desired_state is ChannelDesiredState.STOPPED


def test_delete_requires_confirmation_and_keeps_delivery_audit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    current = store.get_connection(connection_id)
    assert current is not None
    delivery, created = store.begin_delivery(
        connection_id=connection_id,
        platform=ChannelPlatform.TELEGRAM,
        target="chat",
        message="hello",
        idempotency_key="delivery-1",
    )
    assert created is True
    store.complete_delivery(delivery.delivery_id, ChannelSendStatus.ACCEPTED)
    with pytest.raises(ValueError):
        store.delete_connection(
            connection_id,
            expected_revision=current.revision,
            request_id="delete-1",
            confirmed=False,
        )
    result = store.delete_connection(
        connection_id,
        expected_revision=current.revision,
        request_id="delete-1",
        confirmed=True,
    )
    assert result.connection_id == connection_id
    assert store.get_connection(connection_id) is None
    assert [row.status for row in store.list_deliveries(connection_id)] == ["accepted"]
    replay = store.delete_connection(
        connection_id,
        expected_revision=current.revision,
        request_id="delete-1",
        confirmed=True,
    )
    assert replay.replayed is True


def test_inbound_claim_is_unique_per_connection_and_detects_content_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    event = _event(connection_id)
    first = store.claim_inbound(event)
    assert first.created is True
    store.complete_inbound(
        connection_id, event.external_message_id, state="accepted", run_id="run-1"
    )
    duplicate = store.claim_inbound(event)
    assert duplicate.created is False
    assert duplicate.state == "accepted"
    assert duplicate.run_id == "run-1"
    assert duplicate.event_digest == inbound_digest(event)
    changed = _event(connection_id, text="different")
    assert inbound_digest(changed) != duplicate.event_digest


def test_inflight_delivery_becomes_unknown_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    _, created = store.begin_delivery(
        connection_id=connection_id,
        platform=ChannelPlatform.TELEGRAM,
        target="chat",
        message="hello",
        idempotency_key="delivery-unknown",
    )
    assert created is True
    assert store.mark_inflight_deliveries_unknown() == 1
    row = store.list_deliveries(connection_id)[0]
    assert row.status == "unknown"
    assert row.error == "process_interrupted"
    assert (
        store.begin_delivery(
            connection_id=connection_id,
            platform=ChannelPlatform.TELEGRAM,
            target="chat",
            message="hello",
            idempotency_key="delivery-unknown",
        )[1]
        is False
    )


def test_credentials_reject_urls_and_metadata_rejects_secret_keys() -> None:
    with pytest.raises(ValueError):
        validate_credentials({"webhook_url": "https://example.com/hook"})
    with pytest.raises(ValueError):
        validate_credentials({"token": "https://example.com/hook"})
    with pytest.raises(ValueError):
        validate_credentials({"token": ""})
    with pytest.raises(ValueError):
        validate_metadata({"token": "abc"})
    assert validate_credentials({"token": "plain-value"}) == {"token": "plain-value"}


def test_channel_binding_reuses_one_conversation_and_survives_delete(
    tmp_path: Path,
) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = store.get_or_create_channel_conversation(
        workspace=workspace,
        platform=ChannelPlatform.TELEGRAM,
        connection_id="ch-1",
        sender_id="sender",
        conversation_binding="chat",
        initial_prompt="hello",
    )
    again = store.get_or_create_channel_conversation(
        workspace=workspace,
        platform=ChannelPlatform.TELEGRAM,
        connection_id="ch-1",
        sender_id="sender",
        conversation_binding="chat",
        initial_prompt="ignored",
    )
    assert again.conversation_id == first.conversation_id
    assert first.source is ConversationSource.CHANNEL
    other = store.get_or_create_channel_conversation(
        workspace=workspace,
        platform=ChannelPlatform.TELEGRAM,
        connection_id="ch-2",
        sender_id="sender",
        conversation_binding="chat",
        initial_prompt="hello",
    )
    assert other.conversation_id != first.conversation_id
    store.delete_conversation(first.conversation_id)
    assert (
        store.get_channel_conversation(
            workspace=workspace,
            platform=ChannelPlatform.TELEGRAM,
            connection_id="ch-1",
            sender_id="sender",
            conversation_binding="chat",
        )
        is None
    )


def test_allowed_senders_round_trip_and_are_validated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.create_connection(
        platform=ChannelPlatform.TELEGRAM,
        display_name="Home",
        enabled=True,
        owner_identity="owner",
        metadata={},
        credentials=None,
        request_id="senders-create",
        allowed_senders=[" guest ", "guest", "second"],
    )
    assert result.connection.allowed_senders == ("guest", "second")
    updated = store.update_connection(
        result.connection.connection_id,
        expected_revision=1,
        request_id="senders-update",
        changes={"allowed_senders": ["third"]},
    )
    assert updated.connection.allowed_senders == ("third",)
    with pytest.raises(ValueError):
        store.update_connection(
            result.connection.connection_id,
            expected_revision=updated.connection.revision,
            request_id="senders-invalid",
            changes={"allowed_senders": [1]},
        )


def test_schema_v1_store_migrates_the_allowed_senders_column(tmp_path: Path) -> None:
    root = tmp_path / "channels"
    root.mkdir()
    with sqlite3.connect(root / "channels.sqlite3") as database:
        database.executescript(
            """
            CREATE TABLE channel_connections (
                connection_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                desired_state TEXT NOT NULL,
                credential_ref TEXT,
                owner_identity TEXT,
                metadata_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
    store = ChannelStore(root)
    connection_id = _create(store, request_id="migrate-create")
    connection = store.get_connection(connection_id)
    assert connection is not None
    assert connection.allowed_senders == ()
