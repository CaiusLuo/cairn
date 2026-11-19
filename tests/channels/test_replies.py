"""Durable source replies for terminal Channel Runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from cairn.channels.manager import ChannelConnectionView, ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelConnection,
    ChannelDesiredState,
    ChannelInboundEvent,
    ChannelPlatform,
    ChannelRuntimeStatus,
    ChannelSendResult,
    ChannelSendStatus,
)
from cairn.channels.replies import ChannelReplyCoordinator
from cairn.channels.store import ChannelStore, ReplyRecord
from cairn.conversation.models import ConversationSource, Run, RunStatus
from tests.support.pg import database_for


class FakeReplyManager:
    def __init__(
        self,
        connection: ChannelConnection,
        *,
        result: ChannelSendResult | None = None,
        errors: list[BaseException] | None = None,
        actual_state: ChannelActualState = ChannelActualState.RUNNING,
    ) -> None:
        self.connection = connection
        self.actual_state = actual_state
        self.result = result or ChannelSendResult(status=ChannelSendStatus.ACCEPTED)
        self.errors = list(errors or [])
        self.sent: list[tuple[str, str, str, str]] = []

    def get_connection_view(self, connection_id: str) -> ChannelConnectionView:
        return ChannelConnectionView(
            connection=self.connection,
            runtime=ChannelRuntimeStatus(
                connection_id=connection_id,
                actual_state=self.actual_state,
            ),
        )

    async def send_reply(
        self, reply: ReplyRecord, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        self.sent.append((reply.connection_id, reply.target, message, idempotency_key))
        if self.errors:
            raise self.errors.pop(0)
        return self.result


class Runs:
    def __init__(self, runs: dict[str, Run]) -> None:
        self.runs = runs

    def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)


def connection() -> ChannelConnection:
    return ChannelConnection(
        connection_id="ch-1",
        platform=ChannelPlatform.TELEGRAM,
        display_name="Telegram",
        enabled=True,
        desired_state=ChannelDesiredState.RUNNING,
        owner_identity="owner",
    )


def run(
    run_id: str = "run-1",
    *,
    status: RunStatus = RunStatus.COMPLETED,
    result: str | None = "answer",
) -> Run:
    return Run(
        run_id=run_id,
        conversation_id="conv-1",
        workspace_id="ws-1",
        source=ConversationSource.CHANNEL,
        status=status,
        result=result,
    )


def coordinator(
    store: ChannelStore,
    manager: FakeReplyManager,
    runs: Runs,
) -> ChannelReplyCoordinator:
    return ChannelReplyCoordinator(
        manager=cast(ChannelManager, manager),
        runs=runs,
        store=store,
        poll_interval=0.01,
    )


async def settle(tasks: set[asyncio.Task[None]]) -> None:
    if tasks:
        await asyncio.gather(*tuple(tasks))


def test_terminal_run_replies_once_even_when_settled_twice(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        runs = Runs({"run-1": run()})
        replies = coordinator(store, manager, runs)
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(runs.runs["run-1"])
        replies.on_run_settled(runs.runs["run-1"])
        await settle(replies._tasks)
        assert manager.sent == [("ch-1", "chat-7", "answer", "channel-reply:run-1")]
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "sent"

    asyncio.run(scenario())


def test_queued_run_waits_until_it_becomes_terminal(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        queued = run(status=RunStatus.RUNNING)
        runs = Runs({"run-1": queued})
        replies = coordinator(store, manager, runs)
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(queued)
        await settle(replies._tasks)
        assert manager.sent == []
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "pending"

        completed = run()
        runs.runs["run-1"] = completed
        replies.on_run_settled(completed)
        await settle(replies._tasks)
        assert len(manager.sent) == 1

    asyncio.run(scenario())


def test_failed_run_replies_with_an_observable_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        failed = run(status=RunStatus.FAILED, result=None)
        replies = coordinator(store, manager, Runs({"run-1": failed}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(failed)
        await settle(replies._tasks)
        assert manager.sent[0][2] == "本轮执行未完成 (failed)。"

    asyncio.run(scenario())


def test_unknown_send_is_not_replayed_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(
            connection(), result=ChannelSendResult(status=ChannelSendStatus.UNKNOWN)
        )
        terminal = run()
        replies = coordinator(store, manager, Runs({"run-1": terminal}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(terminal)
        await settle(replies._tasks)
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "unknown"

        restarted = coordinator(store, manager, Runs({"run-1": terminal}))
        await restarted.start()
        await settle(restarted._tasks)
        await restarted.close()
        assert len(manager.sent) == 1

    asyncio.run(scenario())


def test_adapter_starting_keeps_the_intent_pending_for_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(
            connection(), errors=[ChannelAdapterError("adapter is not running")]
        )
        terminal = run()
        replies = coordinator(store, manager, Runs({"run-1": terminal}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(terminal)
        await settle(replies._tasks)
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "pending"
        assert record.error is not None and "adapter is not running" in record.error

        await replies._reconcile_pending()
        await settle(replies._tasks)
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "sent"
        assert len(manager.sent) == 2

    asyncio.run(scenario())


def test_connection_still_starting_keeps_the_reply_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(
            connection(), actual_state=ChannelActualState.CONNECTING
        )
        terminal = run()
        replies = coordinator(store, manager, Runs({"run-1": terminal}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(terminal)
        await settle(replies._tasks)
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "pending"
        assert record.error == "connection_not_running"
        assert manager.sent == []

    asyncio.run(scenario())


def test_stopped_connection_fails_the_reply_without_sending(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        stopped = connection().model_copy(
            update={"desired_state": ChannelDesiredState.STOPPED}
        )
        manager = FakeReplyManager(stopped)
        terminal = run()
        replies = coordinator(store, manager, Runs({"run-1": terminal}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        replies.on_run_settled(terminal)
        await settle(replies._tasks)
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "failed"
        assert record.error == "connection_not_running"
        assert manager.sent == []

    asyncio.run(scenario())


def test_missing_run_fails_the_intent_instead_of_retrying_forever(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        replies = coordinator(store, manager, Runs({}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="missing-run",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )
        await replies.start()
        await settle(replies._tasks)
        await replies.close()
        record = store.reply_for_run("missing-run")
        assert record is not None and record.state == "failed"
        assert record.error == "run_not_found"
        assert manager.sent == []

    asyncio.run(scenario())


def test_startup_recreates_a_missing_intent_from_accepted_inbound(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        create = store.create_connection(
            platform=ChannelPlatform.TELEGRAM,
            display_name="Telegram",
            enabled=True,
            owner_identity="owner",
            metadata={},
            credentials={"bot_token": "token-1"},
            request_id="create-1",
        )
        connection_id = create.connection.connection_id
        event = store.claim_inbound(
            ChannelInboundEvent(
                platform=ChannelPlatform.TELEGRAM,
                connection_id=connection_id,
                external_message_id="7:1",
                sender_id="7",
                conversation_binding="7",
                text="hello",
                adapter_identity="bot-1",
            ),
            sender_role="owner",
        )
        assert event.created
        store.complete_inbound(
            connection_id,
            "7:1",
            adapter_identity="bot-1",
            state="accepted",
            conversation_id="conv-1",
            run_id="run-1",
        )
        terminal = run()
        created_connection = store.get_connection(connection_id) or connection()
        manager = FakeReplyManager(
            created_connection.model_copy(
                update={"desired_state": ChannelDesiredState.RUNNING}
            )
        )
        replies = ChannelReplyCoordinator(
            manager=cast(ChannelManager, manager),
            runs=Runs({"run-1": terminal}),
            store=store,
            poll_interval=0.01,
        )
        await replies.start()
        await settle(replies._tasks)
        await replies.close()
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "sent"
        assert manager.sent == [
            (
                connection_id,
                "7",
                "answer",
                "channel-reply:run-1",
            )
        ]

    asyncio.run(scenario())


def test_a_reply_without_trusted_identity_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        terminal = run()
        replies = coordinator(store, manager, Runs({"run-1": terminal}))
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
        )
        await replies.start()
        await settle(replies._tasks)
        await replies.close()
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "failed"
        assert record.error == "reply_identity_missing"
        assert manager.sent == []

    asyncio.run(scenario())


def test_pending_reply_with_identity_survives_restart_and_is_sent_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = ChannelStore(database_for(tmp_path), tmp_path / "channels")
        manager = FakeReplyManager(connection())
        terminal = run()
        store.claim_reply(
            connection_id="ch-1",
            conversation_id="conv-1",
            run_id="run-1",
            target="chat-7",
            external_message_id="7:1",
            sender_id="owner",
            sender_role="owner",
            adapter_identity="bot-1",
        )

        first = coordinator(store, manager, Runs({"run-1": terminal}))
        await first.start()
        await settle(first._tasks)
        await first.close()
        assert len(manager.sent) == 1

        restarted = coordinator(store, manager, Runs({"run-1": terminal}))
        await restarted.start()
        await settle(restarted._tasks)
        await restarted.close()
        assert len(manager.sent) == 1
        record = store.reply_for_run("run-1")
        assert record is not None and record.state == "sent"

    asyncio.run(scenario())
