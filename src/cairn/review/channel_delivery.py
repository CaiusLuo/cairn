"""Push scheduled review reports to enabled, running IM connections.

WeChat keeps its existing proactive-delivery coordinator because that channel
needs an inbound context token. Other channels send through the shared channel
manager, which persists an idempotent delivery record before the network call.
Only scheduled reports with a host delivery intent are pushed; manual previews
remain Web-only.
"""

from __future__ import annotations

import asyncio
import logging

from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelDesiredState,
    ChannelPlatform,
    ChannelSendStatus,
)
from cairn.review.delivery import DEFAULT_MESSAGE_CHARS, format_review_message
from cairn.review.models import ReviewStatus, StoredReview
from cairn.review.store import ReviewStore

LOGGER = logging.getLogger(__name__)


class ReviewChannelDispatcher:
    """Best-effort fan-out with the channel manager's durable idempotency."""

    def __init__(
        self,
        *,
        store: ReviewStore,
        channels: ChannelManager,
        message_chars: int = DEFAULT_MESSAGE_CHARS,
    ) -> None:
        self.store = store
        self.channels = channels
        self.message_chars = message_chars

    async def dispatch(self, review: StoredReview) -> None:
        if review.status is not ReviewStatus.COMPLETED or review.summary is None:
            return
        # A delivery row exists only for the current scheduled period; a
        # compensating historical report has no host intent to notify anyone.
        intent = await asyncio.to_thread(self.store.get_delivery, review.review_id)
        if intent is None:
            return
        body = format_review_message(review, max_chars=self.message_chars)
        for view in self.channels.list_connection_views():
            connection = view.connection
            if connection.legacy or connection.platform is ChannelPlatform.WECHAT:
                continue
            if not connection.enabled:
                continue
            if connection.desired_state is not ChannelDesiredState.RUNNING:
                continue
            if view.runtime.actual_state is not ChannelActualState.RUNNING:
                continue
            owner = connection.owner_identity
            if not owner:
                LOGGER.info(
                    "review delivery skipped %s: owner identity is not configured",
                    connection.connection_id,
                )
                continue
            target = self.channels.latest_inbound_target(
                connection.connection_id, sender_id=owner
            )
            if not target:
                LOGGER.info(
                    "review delivery skipped %s: no accepted owner conversation",
                    connection.connection_id,
                )
                continue
            try:
                result = await self.channels.send(
                    connection.connection_id,
                    target,
                    body,
                    f"review:{review.review_id}:{connection.connection_id}",
                )
            except Exception as exc:
                # A failure on one channel must not block the others or the
                # schedule tick. The channel store records anything that began
                # sending; an uncertain remote result is never replayed here.
                LOGGER.warning(
                    "review delivery failed for %s: %s",
                    connection.connection_id,
                    type(exc).__name__,
                )
                continue
            if result.status is not ChannelSendStatus.ACCEPTED:
                LOGGER.warning(
                    "review delivery %s for %s: %s",
                    result.status.value,
                    connection.connection_id,
                    result.error or "unknown",
                )


__all__ = ["ReviewChannelDispatcher"]
