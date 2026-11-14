"""Review delivery is outbox-based, identity-bound and never auto-replayed."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from cairn.integrations.wechat.session import ProactiveSendOutcome
from cairn.review.delivery import (
    ReviewDeliveryCoordinator,
    format_review_message,
)
from cairn.review.models import (
    DailyReview,
    ReviewDeliveryStatus,
    ReviewItem,
    ReviewStatus,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
)
from cairn.review.store import ReviewStore

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"


def _store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "knowledge")


def _completed_review(store: ReviewStore, request_id: str = "review-1") -> Any:
    created = store.create_or_get(
        request_id=request_id,
        review_type=ReviewType.DAILY,
        day=DAY,
        period_start="2026-01-14T12:30:00+00:00",
        period_end="2026-01-15T12:30:00+00:00",
        timezone=TZ,
        input_fingerprint="a" * 64,
        source_manifest=(),
        collection_status=(
            SourceCollectionStatus(
                source_type="knowledge_clip",
                status=SourceCollectionState.OK,
                discovered=2,
                included=2,
            ),
        ),
    ).review
    store.mark_generating(created.review_id, "test-model")
    summary = DailyReview(
        date=DAY,
        overview="今天完成了可验证的复盘链路。",
        completed_activities=(
            ReviewItem(text="修复了复盘身份问题", source_refs=("clip-1",)),
        ),
        knowledge_insights=(
            ReviewItem(text="投递必须与生成解耦", source_refs=("clip-2",)),
        ),
        recommendations=(ReviewItem(text="明天检查一次投递状态"),),
        confidence_notes=("依据两个 Clip。",),
    )
    return store.complete(
        created.review_id,
        summary=summary,
        summary_markdown="# report\n",
        model_id="test-model",
        request_delivery=True,
    )


class FakeWechat:
    def __init__(
        self,
        *,
        account: str | None = "bot-1",
        recipient: str | None = "owner",
        context: str | None = "ctx-1",
        fail: bool = False,
        outcome: ProactiveSendOutcome = ProactiveSendOutcome.SENT,
    ) -> None:
        self.account = account
        self.recipient = recipient
        self.context = context
        self.fail = fail
        self.outcome = outcome
        self.sent: list[tuple[str, str, str, str]] = []

    @property
    def state(self) -> FakeWechat:
        return self

    def account_id(self) -> str | None:
        return self.account

    def recipient_id(self) -> str | None:
        return self.recipient

    def latest_context(self, _sender: str) -> str | None:
        return self.context

    async def deliver_for_account_outcome(
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
    ) -> ProactiveSendOutcome:
        self.sent.append((sender, text, account_id, context_token or ""))
        if self.fail:
            return ProactiveSendOutcome.UNKNOWN
        return self.outcome

    async def deliver_for_account(
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
    ) -> bool:
        outcome = await self.deliver_for_account_outcome(
            sender, text, account_id, context_token=context_token
        )
        return outcome is ProactiveSendOutcome.SENT


def _coordinator(
    store: ReviewStore,
    wechat: FakeWechat,
    *,
    message_chars: int = 3_500,
    retry_seconds: float = 300.0,
) -> ReviewDeliveryCoordinator:
    return ReviewDeliveryCoordinator(
        store=store,
        wechat=wechat,  # type: ignore[arg-type]
        poll_interval=0.05,
        message_chars=message_chars,
        retry_seconds=retry_seconds,
    )


def test_pending_review_is_sent_once_and_marked_sent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = FakeWechat()
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.SENT
        assert len(wechat.sent) == 1
        assert wechat.sent[0][0] == "owner"
        assert "今天完成了可验证的复盘链路。" in wechat.sent[0][1]
        assert "##" not in wechat.sent[0][1]

        await coordinator._consider(review.review_id)
        assert len(wechat.sent) == 1
        assert store.get(review.review_id).status is ReviewStatus.COMPLETED  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_no_account_or_context_keeps_the_outbox_pending(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    for wechat in (
        FakeWechat(account=None),
        FakeWechat(recipient=None),
        FakeWechat(context=None),
    ):
        coordinator = _coordinator(store, wechat)

        async def scenario(
            current: ReviewDeliveryCoordinator = coordinator,
            fake: FakeWechat = wechat,
        ) -> None:
            await current._consider(review.review_id)
            delivery = store.get_delivery(review.review_id)
            assert delivery is not None
            assert delivery.status is ReviewDeliveryStatus.PENDING
            assert delivery.attempts == 0
            assert fake.sent == []

        asyncio.run(scenario())


def test_account_switch_is_refused_after_identity_is_bound(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    before = _coordinator(store, FakeWechat(account="bot-a", fail=True))

    async def first() -> None:
        await before._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.UNCONFIRMED
        store.requeue_delivery(review.review_id)

    asyncio.run(first())

    after_wechat = FakeWechat(account="bot-b", recipient="owner")
    after = _coordinator(store, after_wechat)

    async def second() -> None:
        await after._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.MANUAL_REVIEW
        assert delivery.last_error == "delivery identity changed"
        assert after_wechat.sent == []

    asyncio.run(second())


def test_failed_send_is_unconfirmed_and_not_retried_automatically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = FakeWechat(fail=True)
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.UNCONFIRMED
        assert delivery.last_error == "send_unconfirmed"
        assert delivery.attempts == 1

        # The review remains completed; only delivery state changed.
        stored_review = store.get(review.review_id)
        assert stored_review is not None
        assert stored_review.status is ReviewStatus.COMPLETED
        assert stored_review.summary is not None

        await coordinator._consider(review.review_id)
        assert len(wechat.sent) == 1
        assert store.get_delivery(review.review_id).status is (  # type: ignore[union-attr]
            ReviewDeliveryStatus.UNCONFIRMED
        )

    asyncio.run(scenario())


def test_rejected_context_stays_pending_until_a_fresh_one_arrives(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = FakeWechat(outcome=ProactiveSendOutcome.REJECTED)
    coordinator = _coordinator(store, wechat, retry_seconds=300)

    async def scenario() -> None:
        await coordinator._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.PENDING
        assert delivery.last_error == "context_token_rejected"
        assert delivery.attempts == 1
        assert len(wechat.sent) == 1

        # A refused context is not an unknown result; retry later rather than
        # requiring manual review, and do not hammer the upstream endpoint.
        await coordinator.start()
        try:
            await asyncio.sleep(0.03)
            assert len(wechat.sent) == 1
            wechat.outcome = ProactiveSendOutcome.SENT
            coordinator.rescan(force=True)
            for _ in range(50):
                if store.get_delivery(review.review_id).status is (  # type: ignore[union-attr]
                    ReviewDeliveryStatus.SENT
                ):
                    break
                await asyncio.sleep(0.01)
            assert store.get_delivery(review.review_id).status is (  # type: ignore[union-attr]
                ReviewDeliveryStatus.SENT
            )
            assert len(wechat.sent) == 2
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_lost_send_response_is_unconfirmed_and_not_resent(
    tmp_path: Path,
) -> None:
    class LostResponseWechat(FakeWechat):
        async def deliver_for_account_outcome(
            self,
            sender: str,
            text: str,
            account_id: str,
            *,
            context_token: str | None = None,
        ) -> ProactiveSendOutcome:
            self.sent.append((sender, text, account_id, context_token or ""))
            raise RuntimeError("response lost after the remote send")

    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = LostResponseWechat()
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.UNCONFIRMED
        assert delivery.last_error == "delivery_error:RuntimeError"
        assert delivery.attempts == 1
        assert len(wechat.sent) == 1

        # The remote side may have accepted the message, so no automatic retry.
        await coordinator._consider(review.review_id)
        assert len(wechat.sent) == 1
        assert store.get(review.review_id).status is ReviewStatus.COMPLETED  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_manual_requeue_retries_after_operator_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = FakeWechat(fail=True)
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator._consider(review.review_id)
        store.requeue_delivery(review.review_id)
        wechat.fail = False
        await coordinator._consider(review.review_id)
        delivery = store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.SENT
        assert delivery.attempts == 2
        assert len(wechat.sent) == 2

    asyncio.run(scenario())


def test_start_recovers_started_attempt_without_sending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    store.begin_delivery(
        review.review_id,
        account_id="bot-1",
        sender_id="owner",
        context_token="ctx-1",
    )
    wechat = FakeWechat()
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator.start()
        try:
            await asyncio.sleep(0.02)
            delivery = store.get_delivery(review.review_id)
            assert delivery is not None
            assert delivery.status is ReviewDeliveryStatus.UNCONFIRMED
            assert wechat.sent == []
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_polling_delivers_pending_review_when_readiness_returns(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    review = _completed_review(store)
    wechat = FakeWechat(context=None)
    coordinator = _coordinator(store, wechat)

    async def scenario() -> None:
        await coordinator.start()
        try:
            await asyncio.sleep(0.03)
            assert wechat.sent == []
            wechat.context = "ctx-late"
            coordinator.rescan()
            for _ in range(50):
                if wechat.sent:
                    break
                await asyncio.sleep(0.01)
            assert len(wechat.sent) == 1
            assert (
                store.get_delivery(review.review_id).status  # type: ignore[union-attr]
                is ReviewDeliveryStatus.SENT
            )
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_message_is_plain_text_mobile_sized_and_truncated(
    tmp_path: Path,
) -> None:
    real = ReviewStore(tmp_path / "knowledge")
    review = _completed_review(real)
    text = format_review_message(review)
    assert "##" not in text and "**" not in text and "`" not in text
    assert "每日复盘" in text
    assert "概览" in text
    assert "今日进展" in text
    assert "来源: knowledge_clip 2" in text
    assert len(text) <= 3_500

    short = format_review_message(review, max_chars=150)
    assert len(short) <= 150
    assert "内容过长" in short
