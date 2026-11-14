"""Host-owned WeChat delivery for completed daily reviews.

Delivery is deliberately separate from review generation and from WeChat login.
A completed review leaves a pending outbox row. The coordinator only sends when
the exact account and recipient are available, persists an attempt before any
network call, and never replays a result that is unknown after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime

from cairn.integrations.wechat.session import (
    ProactiveSendOutcome,
    WechatSessionController,
)
from cairn.review.models import (
    ReviewDeliveryStatus,
    StoredReview,
    StoredReviewDelivery,
)
from cairn.review.store import ReviewStore

DEFAULT_POLL_SECONDS = 30.0
DEFAULT_MAX_PARALLEL = 4
DEFAULT_MESSAGE_CHARS = 3_500
DEFAULT_RETRY_SECONDS = 300.0


def format_review_message(
    record: StoredReview, *, max_chars: int = DEFAULT_MESSAGE_CHARS
) -> str:
    """Render a mobile-friendly plain-text report; never markdown tables."""
    summary = record.summary
    if summary is None:
        raise ValueError("a review must be completed before delivery")
    if max_chars < 120:
        raise ValueError("message limit is too small for a readable review")

    def moment(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
        except ValueError:
            return value

    lines = [
        f"{summary.date.isoformat()} 每日复盘",
        f"窗口 {moment(record.period_start)} → {moment(record.period_end)}",
        "",
        "概览",
        summary.overview,
    ]

    def section(title: str, items: tuple[object, ...]) -> None:
        if not items:
            return
        lines.extend(["", title])
        for index, raw in enumerate(items, start=1):
            text = getattr(raw, "text", "")
            refs = tuple(getattr(raw, "source_refs", ()))
            line = f"{index}. {text}"
            if refs:
                line += f" (依据: {', '.join(refs[:3])})"
            lines.append(line)

    section("今日进展", summary.completed_activities)
    section("知识收获", summary.knowledge_insights)
    section("主要问题", summary.problems)
    section("明日建议", summary.recommendations)

    if summary.confidence_notes:
        lines.extend(["", "说明"])
        lines.extend(f"- {note}" for note in summary.confidence_notes)

    source_bits = [
        f"{status.source_type} {status.included}"
        for status in record.collection_status
        if status.included
    ]
    if source_bits:
        lines.extend(["", "来源: " + " · ".join(source_bits)])

    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    marker = "\n…\n(内容过长, 完整日报见 Web)"
    keep = max_chars - len(marker)
    truncated = text[:keep]
    last_break = truncated.rfind("\n")
    if last_break > keep // 2:
        truncated = truncated[:last_break]
    return truncated.rstrip() + marker


class ReviewDeliveryCoordinator:
    """At-most-once delivery of each completed review through one account."""

    def __init__(
        self,
        *,
        store: ReviewStore,
        wechat: WechatSessionController,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        message_chars: int = DEFAULT_MESSAGE_CHARS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        self.store = store
        self.wechat = wechat
        self.poll_interval = max(0.05, poll_interval)
        self.max_parallel = max(1, max_parallel)
        self.message_chars = message_chars
        self.retry_seconds = max(0.0, retry_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._deferred: dict[str, float] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopped = True

    async def start(self) -> None:
        self._stopped = False
        # A started row means the process may have sent it. Never retry it
        # automatically; an operator must inspect it first.
        await asyncio.to_thread(self.store.recover_started_deliveries)
        self._loop_task = asyncio.create_task(self._loop())
        self.rescan()

    async def close(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def rescan(self, *, force: bool = False) -> None:
        """Wake the coordinator after completion, login or manual retry."""
        if self._stopped:
            return
        if force:
            self._deferred.clear()
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient store or account failure must not kill the loop.
                pass
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)

    async def _scan(self) -> None:
        if self._stopped or self.wechat.account_id() is None:
            return
        recipient = self.wechat.recipient_id()
        if recipient is None or not self.wechat.state.latest_context(recipient):
            return
        pending = await asyncio.to_thread(
            self.store.list_deliveries,
            statuses=frozenset({ReviewDeliveryStatus.PENDING}),
        )
        now = time.monotonic()
        for delivery in pending:
            if len(self._tasks) >= self.max_parallel:
                return
            if delivery.review_id in self._tasks:
                continue
            if now < self._deferred.get(delivery.review_id, 0.0):
                continue
            task = asyncio.create_task(self._consider(delivery.review_id))
            self._tasks[delivery.review_id] = task

            def finished(
                done: asyncio.Task[None], review_id: str = delivery.review_id
            ) -> None:
                self._tasks.pop(review_id, None)
                self.rescan()

            task.add_done_callback(finished)

    async def _consider(self, review_id: str) -> None:
        delivery = await asyncio.to_thread(self.store.get_delivery, review_id)
        if delivery is None or delivery.status is not ReviewDeliveryStatus.PENDING:
            return
        review = await asyncio.to_thread(self.store.get, review_id)
        if review is None or review.summary is None:
            return
        account_id = self.wechat.account_id()
        recipient = self.wechat.recipient_id()
        if not account_id or not recipient:
            return
        context = self.wechat.state.latest_context(recipient)
        if not context:
            # The iLink protocol needs an inbound context token for this
            # proactive send. Stay pending until the account has one.
            return
        attempt = await asyncio.to_thread(
            self.store.begin_delivery,
            review_id,
            account_id=account_id,
            sender_id=recipient,
            context_token=context,
        )
        if not attempt.allowed:
            return
        try:
            body = format_review_message(review, max_chars=self.message_chars)
            outcome = await self.wechat.deliver_for_account_outcome(
                recipient,
                body,
                account_id,
                context_token=context,
            )
            if outcome is ProactiveSendOutcome.SENT:
                await asyncio.to_thread(self.store.mark_delivery_sent, review_id)
            elif outcome in (
                ProactiveSendOutcome.REJECTED,
                ProactiveSendOutcome.UNAVAILABLE,
            ):
                # The remote explicitly refused, or the account/context went
                # away before the call. A fresh inbound context may make the
                # same review deliverable, so keep the outbox row pending.
                error = (
                    "context_token_rejected"
                    if outcome is ProactiveSendOutcome.REJECTED
                    else "send_unavailable"
                )
                await asyncio.to_thread(self.store.defer_delivery, review_id, error)
                self._deferred[review_id] = time.monotonic() + self.retry_seconds
            else:
                await asyncio.to_thread(
                    self.store.mark_delivery_unconfirmed,
                    review_id,
                    "send_unconfirmed",
                )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.mark_delivery_unconfirmed,
                    review_id,
                    "delivery_cancelled",
                )
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.mark_delivery_unconfirmed,
                    review_id,
                    f"delivery_error:{type(exc).__name__}",
                )


def delivery_payload(delivery: StoredReviewDelivery | None) -> dict[str, object] | None:
    if delivery is None:
        return None
    return {
        "review_id": delivery.review_id,
        "status": delivery.status.value,
        "attempts": delivery.attempts,
        "last_error": delivery.last_error,
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
    }


__all__ = [
    "DEFAULT_MESSAGE_CHARS",
    "DEFAULT_RETRY_SECONDS",
    "ReviewDeliveryCoordinator",
    "delivery_payload",
    "format_review_message",
]
