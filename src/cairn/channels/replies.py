"""Durable reply intents for Channel Runs.

A Run may settle in a different process than the one that accepted it. The
intent is therefore stored before the Run can settle, and startup recovery
reconciles accepted inbound messages whose intent was never written. A send
that was in flight when the process stopped is marked unknown and is never
replayed automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelDeliveryUncertain,
    ChannelDesiredState,
    ChannelDisabled,
    ChannelNotFound,
    ChannelSendStatus,
)
from cairn.channels.store import ChannelStore, ReplyRecord
from cairn.conversation.models import ConversationSource, Run, RunStatus
from cairn.shared.lifecycle import settle_owned

TERMINAL = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.PAUSED,
    RunStatus.RECOVERY_REQUIRED,
}


class RunReader(Protocol):
    def get_run(self, run_id: str) -> Run | None: ...


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]


class ChannelReplyCoordinator:
    """Turns terminal Channel Runs into exactly-once source replies."""

    def __init__(
        self,
        *,
        manager: ChannelManager,
        runs: RunReader,
        store: ChannelStore | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.manager = manager
        self.runs = runs
        self.store = store if store is not None else manager.store
        self.poll_interval = max(0.05, poll_interval)
        self._started = False
        self._closed = False
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._recovery_task: asyncio.Task[None] | None = None

    def on_run_settled(self, run: Run) -> None:
        if self._closed or run.source is not ConversationSource.CHANNEL:
            return
        self._wake.set()
        task = asyncio.create_task(self._deliver_run(run.run_id))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("channel reply coordinator already started")
        self._started = True
        self._closed = False
        self.store.mark_inflight_replies_unknown()
        for inbound in self.store.unreplied_inbound():
            self.store.claim_reply(
                connection_id=inbound.connection_id,
                conversation_id=inbound.conversation_id,
                run_id=inbound.run_id,
                target=inbound.target,
                external_message_id=inbound.external_message_id,
            )
        await self._reconcile_pending()
        self._recovery_task = asyncio.create_task(self._recover_loop())

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._recovery_task is not None:
            task, self._recovery_task = self._recovery_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._tasks:
            await settle_owned(*tuple(self._tasks))

    async def _recover_loop(self) -> None:
        while not self._closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            self._wake.clear()
            await self._reconcile_pending()

    async def _reconcile_pending(self) -> None:
        for reply in self.store.list_replies("pending"):
            run = self.runs.get_run(reply.run_id)
            if run is None or run.status not in TERMINAL:
                continue
            await self._deliver(reply, run)

    async def _deliver_run(self, run_id: str) -> None:
        reply = self.store.reply_for_run(run_id)
        run = self.runs.get_run(run_id)
        if reply is None or run is None:
            return
        await self._deliver(reply, run)

    async def _deliver(self, reply: ReplyRecord, run: Run) -> None:
        if reply.state != "pending" or run.status not in TERMINAL:
            return
        try:
            view = self.manager.get_connection_view(reply.connection_id)
        except ChannelNotFound:
            self.store.complete_reply(reply.reply_id, "failed", "connection_not_found")
            return
        if (
            not view.connection.enabled
            or view.connection.desired_state is not ChannelDesiredState.RUNNING
        ):
            self.store.complete_reply(
                reply.reply_id, "failed", "connection_not_running"
            )
            return
        if view.runtime.actual_state is not ChannelActualState.RUNNING:
            # The connection is wanted and is starting or reconnecting. Keep the
            # intent pending so the recovery loop can deliver once it is up.
            self.store.defer_reply(reply.reply_id, "connection_not_running")
            return
        if not self.store.mark_reply_sending(reply.reply_id):
            return
        body = self._body(run)
        try:
            result = await self.manager.send(
                reply.connection_id,
                reply.target,
                body,
                f"channel-reply:{reply.run_id}",
            )
        except ChannelDeliveryUncertain as exc:
            self.store.complete_reply(reply.reply_id, "unknown", _safe_error(exc))
        except (ChannelDisabled, ChannelNotFound) as exc:
            self.store.complete_reply(reply.reply_id, "failed", _safe_error(exc))
        except ChannelAdapterError as exc:
            # The adapter may still be starting or reconnecting. Keep the intent
            # pending so the recovery loop retries while the connection is up.
            self.store.defer_reply(reply.reply_id, _safe_error(exc))
        except Exception as exc:
            self.store.complete_reply(reply.reply_id, "failed", _safe_error(exc))
        else:
            if result.status is ChannelSendStatus.ACCEPTED:
                self.store.complete_reply(reply.reply_id, "sent")
            elif result.status is ChannelSendStatus.REJECTED:
                self.store.complete_reply(reply.reply_id, "failed", result.error)
            else:
                self.store.complete_reply(reply.reply_id, "unknown", result.error)

    @staticmethod
    def _body(run: Run) -> str:
        if run.status is RunStatus.COMPLETED:
            return run.result or "本轮没有生成可显示回复。"
        return f"本轮执行未完成 ({run.status.value})。"


__all__ = ["ChannelReplyCoordinator"]
