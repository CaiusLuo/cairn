"""Scheduled review delivery through the real iLink adapter over local HTTP."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from cairn.integrations.wechat.session import WechatSessionController
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.review.delivery import ReviewDeliveryCoordinator
from cairn.review.models import (
    DailyReview,
    DeliveryTarget,
    ReviewDeliveryStatus,
    ReviewItem,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
)
from cairn.review.store import ReviewStore
from tests.support.fake_ilink import FakeILink

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"


def _completed_review(
    store: ReviewStore, target: DeliveryTarget, request_id: str
) -> Any:
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
                discovered=1,
                included=1,
            ),
        ),
    ).review
    store.mark_generating(created.review_id, "test-model")
    summary = DailyReview(
        date=DAY,
        overview="通过真实适配器发送的日报。",
        completed_activities=(
            ReviewItem(text="完成投递验证", source_refs=("clip-1",)),
        ),
    )
    return store.complete(
        created.review_id,
        summary=summary,
        summary_markdown="# report\n",
        model_id="test-model",
        request_delivery=True,
        delivery_target=target,
    )


def _controller(
    tmp_path: Path,
    fake: FakeILink,
    *,
    recipient: str,
) -> WechatSessionController:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    state.save_account(
        WechatAccount(
            bot_token="bot-token",
            ilink_bot_id="bot-a",
            base_url=fake.url,
            ilink_user_id=recipient,
        )
    )
    state.record_received(
        "msg:1",
        sender=recipient,
        context_token="ctx-1",
        text="hello",
    )

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> Any:  # pragma: no cover - never used by delivery
        raise AssertionError("delivery must not execute a task")

    return WechatSessionController(
        workspace=workspace,
        state_dir=state_dir,
        task_executor=executor,
        emit=lambda _text: None,
        allow_insecure_local_http=True,
    )


def _coordinator(
    tmp_path: Path,
    controller: WechatSessionController,
    target: DeliveryTarget,
) -> tuple[ReviewDeliveryCoordinator, ReviewStore]:
    store = ReviewStore(tmp_path / "knowledge")
    return (
        ReviewDeliveryCoordinator(
            store=store,
            wechat=controller,
            target=target,
            poll_interval=0.05,
        ),
        store,
    )


def test_authorized_review_is_sent_through_real_ilink_http(tmp_path: Path) -> None:
    target = DeliveryTarget(account_id="bot-a", recipient="owner")
    with FakeILink() as fake:
        controller = _controller(tmp_path, fake, recipient="owner")
        coordinator, store = _coordinator(tmp_path, controller, target)
        review = _completed_review(store, target, "ilink-delivery")

        async def scenario() -> None:
            await coordinator.start()
            try:
                # Receiving the HTTP request precedes acknowledging and settling it.
                async with asyncio.timeout(5):
                    while True:
                        delivery = store.get_delivery(review.review_id)
                        if delivery is not None and delivery.status is (
                            ReviewDeliveryStatus.ACCEPTED
                        ):
                            break
                        await asyncio.sleep(0.01)
                assert len(fake.sent) == 1
                assert fake.sent[0]["to_user_id"] == "owner"
                assert fake.sent[0]["context_token"] == "ctx-1"
                text = fake.sent[0]["item_list"][0]["text_item"]["text"]
                assert text.startswith("Cairn · 每日复盘")
            finally:
                await coordinator.close()

        asyncio.run(scenario())


def test_account_switched_to_another_user_never_sends(tmp_path: Path) -> None:
    target = DeliveryTarget(account_id="bot-a", recipient="owner")
    with FakeILink() as fake:
        controller = _controller(tmp_path, fake, recipient="intruder")
        coordinator, store = _coordinator(tmp_path, controller, target)
        review = _completed_review(store, target, "ilink-switched")

        async def scenario() -> None:
            await coordinator.start()
            try:
                await asyncio.sleep(0.1)
                assert fake.sent == []
                assert not any(
                    request["path"] == "/ilink/bot/sendmessage"
                    for request in fake.requests
                )
                delivery = store.get_delivery(review.review_id)
                assert delivery is not None
                assert delivery.status is ReviewDeliveryStatus.PENDING
                assert delivery.attempts == 0
            finally:
                await coordinator.close()

        asyncio.run(scenario())


def test_explicit_rejection_from_ilink_stays_retryable(tmp_path: Path) -> None:
    target = DeliveryTarget(account_id="bot-a", recipient="owner")
    with FakeILink() as fake:
        fake.send_ret = -2
        controller = _controller(tmp_path, fake, recipient="owner")
        coordinator, store = _coordinator(tmp_path, controller, target)
        review = _completed_review(store, target, "ilink-rejected")

        async def scenario() -> None:
            await coordinator.start()
            try:
                for _ in range(200):
                    delivery = store.get_delivery(review.review_id)
                    if delivery is not None and delivery.status is (
                        ReviewDeliveryStatus.BLOCKED_OFFLINE
                    ):
                        break
                    await asyncio.sleep(0.01)
                delivery = store.get_delivery(review.review_id)
                assert delivery is not None
                assert delivery.status is ReviewDeliveryStatus.BLOCKED_OFFLINE
                assert delivery.last_error == "context_token_rejected"
                assert fake.sent
            finally:
                await coordinator.close()

        asyncio.run(scenario())
