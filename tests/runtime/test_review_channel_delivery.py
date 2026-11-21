"""Scheduled reviews fan out to running IM connections exactly once."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterHealth,
    ChannelConnection,
    ChannelDesiredState,
    ChannelPlatform,
    ChannelSendResult,
    ChannelSendStatus,
)
from cairn.knowledge.store import KnowledgeStore
from cairn.review.channel_delivery import ReviewChannelDispatcher
from cairn.review.delivery import format_review_message
from cairn.review.models import (
    DailyReview,
    DeliveryTarget,
    ReviewItem,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
)
from cairn.review.store import ReviewStore
from tests.channels.support import inbound, make_manager
from tests.support.pg import database_for

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"


class _LegacyWechat:
    """The host-owned WeChat adapter must never receive a second push."""

    externally_managed = True
    platform = ChannelPlatform.WECHAT
    connection_id = "legacy-wechat"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def connection(self) -> ChannelConnection:
        return ChannelConnection(
            connection_id=self.connection_id,
            platform=ChannelPlatform.WECHAT,
            display_name="WeChat",
            enabled=True,
            desired_state=ChannelDesiredState.RUNNING,
            owner_identity="sender-1",
            legacy=True,
        )

    def health(self) -> ChannelAdapterHealth:
        return ChannelAdapterHealth(actual_state=ChannelActualState.RUNNING)

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        self.sent.append((target, message, idempotency_key))
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)


def _review(
    tmp_path: Path, *, request_delivery: bool = True
) -> tuple[ReviewStore, Any]:
    store = ReviewStore(KnowledgeStore(database_for(tmp_path / "review")))
    created = store.create_or_get(
        request_id="channel-delivery",
        review_type=ReviewType.DAILY,
        day=DAY,
        period_start="2026-01-14T12:30:00+00:00",
        period_end="2026-01-15T12:30:00+00:00",
        timezone=TZ,
        input_fingerprint="c" * 64,
        source_manifest=(),
        collection_status=(
            SourceCollectionStatus(
                source_type="conversation",
                status=SourceCollectionState.OK,
                discovered=1,
                included=1,
            ),
        ),
    ).review
    store.mark_generating(created.review_id, "test-model")
    summary = DailyReview(
        date=DAY,
        overview="今天讨论了备份方案。",
        conversation_highlights=(
            ReviewItem(text="确认了每日备份窗口。", source_refs=("run-1",)),
        ),
        knowledge_recall=(
            ReviewItem(text="旧手册记录了 pg_dump 流程。", source_refs=("recall-1",)),
        ),
        confidence_notes=("依据一次已完成对话。",),
    )
    return store, store.complete(
        created.review_id,
        summary=summary,
        summary_markdown="# report\n",
        model_id="test-model",
        request_delivery=request_delivery,
        delivery_target=(
            DeliveryTarget(recipient="owner") if request_delivery else None
        ),
    )


def test_scheduled_review_is_pushed_to_a_running_connection_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, factory = make_manager(tmp_path / "channels")
        telegram = await manager.create_connection(
            platform=ChannelPlatform.TELEGRAM,
            display_name="Telegram",
            enabled=True,
            owner_identity="sender-1",
            metadata={},
            credentials=None,
            request_id="create-telegram",
        )
        connection_id = telegram.connection.connection_id
        await manager.start_connection(connection_id, "start-telegram")
        accepted = await manager.accept_inbound(inbound(connection_id, "m-1"))
        assert accepted.state == "accepted"

        store, review = _review(tmp_path)
        dispatcher = ReviewChannelDispatcher(store=store, channels=manager)
        try:
            await dispatcher.dispatch(review)
            adapter = factory.adapters[connection_id]
            assert adapter.sent == [
                (
                    "chat-1",
                    format_review_message(review),
                    f"review:{review.review_id}:{connection_id}",
                )
            ]
            message = adapter.sent[0][1]
            assert "对话亮点" in message
            assert "知识召回" in message
            assert "确认了每日备份窗口。" in message
            assert "旧手册记录了 pg_dump 流程。" in message

            # A repeated callback finds the accepted durable delivery and does
            # not send the same report to the chat again.
            await dispatcher.dispatch(review)
            assert len(adapter.sent) == 1
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_manual_reviews_and_unsafe_connections_are_not_pushed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, factory = make_manager(tmp_path / "channels")
        legacy = _LegacyWechat()
        manager.attach_legacy(legacy)

        stopped = await manager.create_connection(
            platform=ChannelPlatform.DISCORD,
            display_name="Discord",
            enabled=True,
            owner_identity="sender-1",
            metadata={},
            credentials=None,
            request_id="create-discord",
        )
        ownerless = await manager.create_connection(
            platform=ChannelPlatform.QQ,
            display_name="QQ",
            enabled=True,
            owner_identity=None,
            metadata={},
            credentials=None,
            request_id="create-qq",
        )
        disabled = await manager.create_connection(
            platform=ChannelPlatform.FEISHU,
            display_name="Feishu",
            enabled=False,
            owner_identity="sender-1",
            metadata={},
            credentials=None,
            request_id="create-feishu",
        )
        await manager.start_connection(ownerless.connection.connection_id, "start-qq")

        store, review = _review(tmp_path)
        manual_store, manual = _review(tmp_path / "manual", request_delivery=False)
        dispatcher = ReviewChannelDispatcher(store=store, channels=manager)
        manual_dispatcher = ReviewChannelDispatcher(
            store=manual_store, channels=manager
        )
        try:
            await dispatcher.dispatch(review)
            await manual_dispatcher.dispatch(manual)
            assert legacy.sent == []
            assert factory.adapters[ownerless.connection.connection_id].sent == []
            assert stopped.connection.connection_id not in factory.adapters
            assert disabled.connection.connection_id not in factory.adapters
        finally:
            await manager.close()

    asyncio.run(scenario())
