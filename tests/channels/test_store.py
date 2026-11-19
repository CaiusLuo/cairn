"""Durable channel configuration, receipts, inbound dedupe and delivery audit."""

from __future__ import annotations

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
from tests.support.pg import database_for


def _store(path: Path) -> ChannelStore:
    return ChannelStore(database_for(path), path / "channels")


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
    connection_id: str,
    message_id: str = "m-1",
    text: str = "hi",
    *,
    adapter_identity: str | None = None,
) -> ChannelInboundEvent:
    return ChannelInboundEvent(
        platform=ChannelPlatform.TELEGRAM,
        connection_id=connection_id,
        external_message_id=message_id,
        sender_id="sender",
        conversation_binding="chat",
        text=text,
        adapter_identity=adapter_identity,
    )


def test_store_is_private_and_credentials_are_not_in_connection_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    assert store.root.stat().st_mode & 0o777 == 0o700
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
    # JSONB cannot hold invalid JSON, so corruption is a valid value of the
    # wrong shape.
    with database_for(tmp_path).connect("channel") as db:
        db.execute(
            "INSERT INTO channel_requests "
            "(request_id, operation, digest, state, result_json, created_at, updated_at) "
            "VALUES ('corrupt-shape', 'update', 'digest', 'completed', '[]', '', '')"
        )
    with pytest.raises(RuntimeError):
        store.request_receipt("corrupt-shape")


def test_completed_request_receipts_are_pruned_only_after_retention(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.claim_request("retained", operation="update", digest_value={"probe": 1})
    store.complete_request("retained", {"connection": {"revision": 1}})
    with database_for(tmp_path).connect("channel") as connection:
        connection.execute(
            "UPDATE channel_requests SET updated_at = %s WHERE request_id = %s",
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


def test_inbound_identity_separates_colliding_message_ids_from_different_bots(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    first = _event(connection_id, "7:42", "hello", adapter_identity="bot-a")
    assert store.claim_inbound(first, request_id="request-a").created is True
    store.complete_inbound(
        connection_id,
        "7:42",
        adapter_identity="bot-a",
        state="accepted",
        run_id="run-a",
    )
    duplicate = store.claim_inbound(first, request_id="request-a")
    assert duplicate.created is False
    assert duplicate.run_id == "run-a"
    assert duplicate.request_id == "request-a"

    # Identical text is not proof of the same message when another Bot owns
    # the update-ID namespace.
    second = _event(connection_id, "7:42", "hello", adapter_identity="bot-b")
    claimed = store.claim_inbound(second, request_id="request-b")
    assert claimed.created is True
    assert claimed.request_id == "request-b"
    store.complete_inbound(
        connection_id,
        "7:42",
        adapter_identity="bot-b",
        state="accepted",
        run_id="run-b",
    )
    repeated = store.claim_inbound(second)
    assert repeated.created is False
    assert repeated.run_id == "run-b"
    with database_for(tmp_path).connect("channel") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM channel_inbound WHERE connection_id = %s",
            (connection_id,),
        ).fetchone()[0]
    assert count == 2


def test_adoption_binds_only_inbound_rows_after_the_current_credential(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store, credentials={"bot_token": "123456:" + "a" * 35})
    current = _event(connection_id, "7:42", "hello")
    assert store.claim_inbound(current).created is True
    store.complete_inbound(
        connection_id,
        "7:42",
        state="accepted",
        run_id="run-current",
    )
    store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv",
        run_id="run-current",
        target="7",
        external_message_id="7:42",
        sender_id="sender",
        sender_role="owner",
    )
    older = _event(connection_id, "7:41", "older")
    assert store.claim_inbound(older).created is True
    store.complete_inbound(
        connection_id,
        "7:41",
        state="accepted",
        run_id="run-older",
    )
    with database_for(tmp_path).connect("channel") as connection:
        connection.execute(
            "UPDATE channel_inbound SET received_at = %s "
            "WHERE connection_id = %s AND external_message_id = %s",
            ("2000-01-01T00:00:00+00:00", connection_id, "7:41"),
        )

    assert store.adopt_unidentified_inbound(connection_id, "bot-a") == 1
    adopted = store.claim_inbound(
        _event(connection_id, "7:42", "hello", adapter_identity="bot-a")
    )
    assert adopted.created is False
    assert adopted.run_id == "run-current"
    reply = store.reply_for_run("run-current")
    assert reply is not None and reply.adapter_identity == "bot-a"

    # A row from before the current token cannot be proven. It stays in the
    # legacy namespace and never suppresses Bot B's colliding update ID.
    unproven = store.claim_inbound(
        _event(connection_id, "7:41", "older", adapter_identity="bot-b")
    )
    assert unproven.created is True
    assert store.adopt_unidentified_inbound(connection_id, "bot-b") == 0


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
    store = DomainStore(database_for(tmp_path))
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


def test_reply_intents_are_idempotent_and_conflict_by_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    first = store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv-1",
        run_id="run-1",
        target="chat",
        external_message_id="m-1",
    )
    again = store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv-1",
        run_id="run-1",
        target="chat",
        external_message_id="m-1",
    )
    assert first == again
    assert store.reply_for_run("run-1") == first
    with pytest.raises(ChannelRequestConflict):
        store.claim_reply(
            connection_id=connection_id,
            conversation_id="conv-1",
            run_id="run-2",
            target="chat",
            external_message_id="m-1",
        )


def test_reply_send_claims_are_atomic_and_interruptions_are_unknown(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    record = store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv-1",
        run_id="run-1",
        target="chat",
        external_message_id="m-1",
    )
    assert store.mark_reply_sending(record.reply_id) is True
    assert store.mark_reply_sending(record.reply_id) is False
    in_flight = store.reply_for_run("run-1")
    assert in_flight is not None and in_flight.state == "sending"
    assert in_flight.attempts == 1

    assert store.mark_inflight_replies_unknown() == 1
    interrupted = store.reply_for_run("run-1")
    assert interrupted is not None and interrupted.state == "unknown"
    assert interrupted.error == "process_interrupted"
    assert store.list_replies("pending") == ()


def test_deferred_reply_stays_pending_with_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    record = store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv-1",
        run_id="run-1",
        target="chat",
        external_message_id="m-1",
    )
    store.mark_reply_sending(record.reply_id)
    store.defer_reply(record.reply_id, "adapter is not running")
    deferred = store.reply_for_run("run-1")
    assert deferred is not None and deferred.state == "pending"
    assert deferred.error == "adapter is not running"
    assert store.list_replies("pending") == (deferred,)

    store.complete_reply(record.reply_id, "sent")
    sent = store.reply_for_run("run-1")
    assert sent is not None and sent.state == "sent"


def test_unreplied_inbound_lists_only_accepted_runs_without_an_intent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    claimed = store.claim_inbound(_event(connection_id, "m-1"))
    assert claimed.created
    store.complete_inbound(
        connection_id,
        "m-1",
        state="accepted",
        conversation_id="conv-1",
        run_id="run-1",
    )
    rejected = store.claim_inbound(_event(connection_id, "m-2"))
    assert rejected.created
    store.complete_inbound(connection_id, "m-2", state="rejected", error="no")

    rows = store.unreplied_inbound()
    assert len(rows) == 1
    assert rows[0].connection_id == connection_id
    assert rows[0].run_id == "run-1"
    assert rows[0].target == "chat"
    assert rows[0].external_message_id == "m-1"

    store.claim_reply(
        connection_id=connection_id,
        conversation_id="conv-1",
        run_id="run-1",
        target="chat",
        external_message_id="m-1",
    )
    assert store.unreplied_inbound() == ()


def test_adapter_state_roundtrips_and_ignores_corrupt_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    assert store.load_adapter_state(connection_id, "telegram") is None
    store.save_adapter_state(connection_id, "telegram", {"offset": 42})
    assert store.load_adapter_state(connection_id, "telegram") == {"offset": 42}
    store.save_adapter_state(connection_id, "telegram", {"offset": 43})
    assert store.load_adapter_state(connection_id, "telegram") == {"offset": 43}

    with database_for(tmp_path).connect("channel") as connection:
        connection.execute(
            "UPDATE channel_adapter_state SET value_json = '[]' "
            "WHERE connection_id = %s",
            (connection_id,),
        )
    assert store.load_adapter_state(connection_id, "telegram") is None


def test_inbound_inbox_dedupes_retries_and_keeps_terminal_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    event = _event(connection_id, "m-inbox", adapter_identity="bot-a")
    created, state = store.enqueue_inbound_inbox(
        event, pending_limit=10, window_seconds=60.0
    )
    assert created is True and state == "pending"
    duplicate, duplicate_state = store.enqueue_inbound_inbox(
        event, pending_limit=10, window_seconds=60.0
    )
    assert duplicate is False and duplicate_state == "pending"
    conflict, conflict_state = store.enqueue_inbound_inbox(
        _event(connection_id, "m-inbox", "different", adapter_identity="bot-a"),
        pending_limit=10,
        window_seconds=60.0,
    )
    assert conflict is False and conflict_state == "conflict"

    due = store.due_inbound_inbox(connection_id, "bot-a")
    assert len(due) == 1
    assert due[0].event.text == "hi"
    store.record_inbound_inbox_attempt(
        connection_id,
        "bot-a",
        "m-inbox",
        error="database is locked",
        next_attempt_at=0,
    )
    entry = store.inbound_inbox_entry(connection_id, "bot-a", "m-inbox")
    assert entry is not None
    assert entry.attempts == 1
    assert entry.last_error == "database is locked"
    assert len(store.due_inbound_inbox(connection_id, "bot-a")) == 1

    assert (
        store.settle_inbound_inbox(connection_id, "bot-a", "m-inbox", state="admitted")
        is True
    )
    assert (
        store.settle_inbound_inbox(connection_id, "bot-a", "m-inbox", state="rejected")
        is False
    )
    admitted = store.inbound_inbox_entry(connection_id, "bot-a", "m-inbox")
    assert admitted is not None and admitted.state == "admitted"
    assert store.inbound_inbox_summary(connection_id) == {
        "admitted": 1,
        "total": 1,
    }


def test_inbound_inbox_pending_limit_is_an_explicit_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connection_id = _create(store)
    states = [
        store.enqueue_inbound_inbox(
            _event(connection_id, f"m-{index}", adapter_identity="bot-a"),
            pending_limit=2,
            window_seconds=60.0,
        )[1]
        for index in range(3)
    ]
    assert states == ["pending", "pending", "failed"]
    summary = store.inbound_inbox_summary(connection_id, "bot-a")
    assert summary == {"pending": 2, "failed": 1, "total": 3}
    failed = store.inbound_inbox_entry(connection_id, "bot-a", "m-2")
    assert failed is not None
    assert failed.last_error == "inbound_pending_limit"
    assert store.due_inbound_inbox(connection_id, "bot-a")[0].event.text == "hi"
