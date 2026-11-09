"""Server-owned delivery of finished WeChat conversation runs."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib

from cairn.core.domain import Run
from cairn.integrations.wechat.session import WechatSessionController
from cairn.integrations.wechat.state import WechatState
from cairn.runtime.conversation_service import ConversationService

TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "paused", "cancelled", "recovery_required"}
)


class WechatDeliveryCoordinator:
    """Deliver each accepted Run result at most once through WeChat."""

    def __init__(
        self,
        *,
        conversations: ConversationService,
        wechat: WechatSessionController,
        state: WechatState,
        max_parallel: int = 16,
    ) -> None:
        self.conversations = conversations
        self.wechat = wechat
        self.state = state
        self.max_parallel = max_parallel
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        self._stopped = False
        self.conversations.add_run_settled_listener(self._on_run_settled)
        self.state.add_delivery_listener(self._on_intent_changed)
        self._recovery_task = asyncio.create_task(self._recover())

    async def close(self) -> None:
        self._stopped = True
        self.conversations.remove_run_settled_listener(self._on_run_settled)
        self.state.remove_delivery_listener(self._on_intent_changed)
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def _on_run_settled(self, run: Run) -> None:
        if self._stopped:
            return
        self._schedule_for_conversation(run.conversation_id)

    def _on_intent_changed(self, key: str) -> None:
        if self._stopped:
            return
        self._schedule(key)

    async def _recover(self) -> None:
        # A started record was in the unknown window when the process stopped.
        # It must never be sent automatically again.
        for intent in self.state.list_delivery_intents():
            if intent.get("delivery_status") == "started":
                with contextlib.suppress(Exception):
                    self.state.mark_delivery_unconfirmed(intent["intent_key"])
            elif intent.get("delivery_status") == "pending":
                self._schedule(intent["intent_key"])

    def rescan(self) -> None:
        """Re-check pending intents after account or sender readiness changes."""
        if self._stopped:
            return
        for intent in self.state.list_delivery_intents(statuses=frozenset({"pending"})):
            if intent.get("needs_manual_review"):
                continue
            self._schedule(intent["intent_key"])

    def _schedule_for_conversation(self, conversation_id: str) -> None:
        for intent in self.state.list_delivery_intents(statuses=frozenset({"pending"})):
            if intent.get("conversation_id") == conversation_id:
                self._schedule(intent["intent_key"])

    def _schedule(self, key: str) -> None:
        if self._stopped or key in self._tasks or len(self._tasks) >= self.max_parallel:
            return
        task = asyncio.create_task(self._consider(key))
        self._tasks[key] = task

        def finished(_done: asyncio.Task[None]) -> None:
            self._tasks.pop(key, None)
            self._schedule_more()

        task.add_done_callback(finished)

    def _schedule_more(self) -> None:
        if (
            self._stopped
            or len(self._tasks) >= self.max_parallel
            or self.wechat.account_id() is None
        ):
            return
        for intent in self.state.list_delivery_intents(statuses=frozenset({"pending"})):
            if len(self._tasks) >= self.max_parallel:
                return
            key = intent["intent_key"]
            if key in self._tasks or intent.get("needs_manual_review"):
                continue
            run_id = intent.get("run_id")
            if run_id is None:
                continue
            run = self.conversations.get_run(run_id)
            if run is None or run.status.value not in TERMINAL_RUN_STATUSES:
                continue
            self._schedule(key)

    async def _consider(self, key: str) -> None:
        intent = self.state.get_delivery_intent(key)
        if intent is None or intent.get("delivery_status") != "pending":
            return
        if intent.get("needs_manual_review"):
            return

        conversation_id = intent.get("conversation_id")
        request_id = intent.get("request_id")
        message_key = str(intent.get("message_key", ""))
        message = self.state.get_record(message_key) if message_key else None
        account_id = self.wechat.account_id() or ""
        sender = str(message.get("sender", "")) if message else ""

        if conversation_id is None:
            if not message or not account_id or not sender:
                # Account/sender readiness is not a delivery attempt.  Keep
                # the intent pending and let the account listener rescan it.
                return
            conversation = self.conversations.get_wechat_conversation(
                account_id, sender
            )
            if conversation is None:
                return
            self.state.bind_delivery_intent(
                key, conversation_id=conversation.conversation_id
            )
            conversation_id = conversation.conversation_id

        run_id = intent.get("run_id")
        if run_id is None and conversation_id and request_id:
            run = self.conversations.run_by_request(conversation_id, request_id)
            if run is not None:
                self.state.bind_delivery_intent(
                    key, conversation_id=conversation_id, run_id=run.run_id
                )
                run_id = run.run_id
        if run_id is None:
            return
        run = self.conversations.get_run(run_id)
        if run is None or run.status.value not in TERMINAL_RUN_STATUSES:
            return

        if message is None:
            self.state.mark_delivery_manual_review(
                key, "message record missing before delivery"
            )
            return
        context = str(intent.get("context_token") or message.get("context_token") or "")
        if not sender or not context or not account_id:
            # An account or context that is temporarily unavailable is not a
            # send attempt; preserve pending until readiness is restored.
            return
        account_digest = hashlib.sha256(account_id.encode()).hexdigest()
        sender_digest = hashlib.sha256(sender.encode()).hexdigest()
        if account_digest != intent.get(
            "account_digest"
        ) or sender_digest != intent.get("sender_digest"):
            self.state.mark_delivery_manual_review(
                key, "delivery identity does not match intent"
            )
            return

        if not self.state.begin_delivery_attempt(key):
            return
        try:
            delivered = await self.wechat.deliver_for_account(
                sender,
                _delivery_body(run),
                account_id,
                context_token=context,
            )
            if delivered:
                self.state.mark_delivery_sent(key)
            else:
                self.state.mark_delivery_unconfirmed(key)
        except Exception:
            with contextlib.suppress(Exception):
                self.state.mark_delivery_unconfirmed(key)


def _delivery_body(run: Run) -> str:
    status = run.status.value
    if status == "completed":
        return run.result or "本轮没有生成可显示回复。"
    if status == "paused":
        return "任务已暂停。"
    if status == "cancelled":
        return "任务已取消。"
    if status == "recovery_required":
        return "本轮执行需要恢复核对, 请发送 /status 查询。"
    if status == "failed":
        return "本轮执行失败, 请发送 /status 查询。"
    return "任务状态: " + status
