"""Wechat text adapter over the same authenticated Runtime HTTP API."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from typing import Any
from uuid import uuid4

import httpx

from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.wechat.state import WechatState


def wechat_request_id(account_id: str, sender_id: str, message_id: str) -> str:
    return (
        "wechat:"
        + hashlib.sha256(
            (account_id + "\0" + sender_id + "\0" + message_id).encode()
        ).hexdigest()
    )


class WechatConversationAdapter:
    """Run ordinary WeChat chat through the Conversation/Run API.

    The existing WechatRuntimeAdapter handles explicit commands and development
    tasks.  This adapter owns only ordinary chat.
    """

    def __init__(
        self,
        client: RuntimeClient,
        *,
        legacy: WechatRuntimeAdapter | None = None,
        delivery_state: WechatState | None = None,
    ) -> None:
        self.client = client
        self.legacy = legacy or WechatRuntimeAdapter(client)
        self.delivery_state = delivery_state

    def _set_admission(
        self, delivery_key: str | None, status: str, error: str | None = None
    ) -> None:
        if self.delivery_state is not None and delivery_key is not None:
            self.delivery_state.set_admission_status(delivery_key, status, error=error)

    def _bind(self, delivery_key: str | None, **fields: str) -> None:
        if self.delivery_state is not None and delivery_key is not None:
            self.delivery_state.bind_delivery_intent(delivery_key, **fields)

    async def reply(
        self,
        text: str,
        *,
        message_id: str | None = None,
        conversation_key: str | None = None,
        sender: str | None = None,
        account: str | None = None,
        deliver: bool = True,
        delivery_key: str | None = None,
        request_id: str | None = None,
    ) -> str:
        del conversation_key
        content = text.strip()
        if not content:
            return "消息内容为空。"
        if content.startswith("/"):
            command, _, argument = content.partition(" ")
            if command == "/status":
                return await self._status_reply(account, sender, argument)
            if command == "/task" and argument.startswith("task-"):
                return await self._task_query_reply(account, sender, argument)
            return await self.legacy.reply(
                text,
                message_id=message_id,
                sender=sender,
                account=account,
            )
        if not sender or not account:
            return "无法确认微信身份, 消息未执行。"
        if not message_id:
            return "消息缺少稳定标识, 未执行。"
        request_id = request_id or wechat_request_id(account, sender, message_id)
        conversation: dict[str, Any] | None = None
        try:
            conversation = await self.client.ensure_wechat_conversation(
                account, sender, content
            )
            self._bind(delivery_key, conversation_id=conversation["conversation_id"])
            run = await self.client.send_conversation(
                conversation["conversation_id"],
                content,
                source="wechat",
                request_id=request_id,
            )
            self._bind(
                delivery_key,
                conversation_id=conversation["conversation_id"],
                run_id=run["run_id"],
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                code = ""
                with suppress(ValueError):
                    code = str(error.response.json().get("error", ""))
                self._set_admission(delivery_key, "rejected", code or "conflict")
                if code == "conversation_busy":
                    return "上一条消息仍在处理中, 请稍后重发。"
                if code == "conversation_recovery_required":
                    return "上一条消息需要恢复核对, 请发送 /status 查询。"
                if code == "invalid_request":
                    return "消息与已有请求不一致, 未执行。"
                return "消息未被接受, 请发送 /status 查询。"
            self._set_admission(delivery_key, "admission_unknown", type(error).__name__)
            return "消息接收状态未知, 请稍后核对, 不会自动重试。"
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if conversation is not None:
                try:
                    found = await self.client.find_run_by_request(
                        conversation["conversation_id"], request_id
                    )
                except httpx.HTTPError:
                    found = None
                if found is not None:
                    self._set_admission(delivery_key, "accepted", type(error).__name__)
                    self._bind(
                        delivery_key,
                        conversation_id=conversation["conversation_id"],
                        run_id=found["run_id"],
                    )
                    if deliver:
                        return self._final_text(await self._wait_run(found["run_id"]))
                    return ""
            self._set_admission(delivery_key, "admission_unknown", type(error).__name__)
            return "消息接收状态未知, 请稍后核对, 不会自动重试。"
        if not deliver:
            return ""
        final = await self._wait_run(run["run_id"])
        return self._final_text(final)

    async def _status_reply(
        self, account: str | None, sender: str | None, argument: str
    ) -> str:
        if not account or not sender:
            return "无法确认微信身份, 未执行。"
        argument = argument.strip()
        if argument.startswith("task-"):
            return await self._task_query_reply(account, sender, argument)
        try:
            binding = await self.client.get_wechat_conversation(account, sender)
            conversation_id = str(binding["conversation_id"])
            if argument:
                run = await self.client.get_run(argument)
                if str(run.get("conversation_id", "")) != conversation_id:
                    return "未找到对应的 Run。"
            else:
                detail = await self.client.get_conversation(conversation_id)
                runs = detail.get("runs") or []
                if not runs:
                    return "当前没有对话记录。"
                run = runs[-1]
        except (httpx.HTTPStatusError, KeyError, ValueError):
            return "未找到对应的 Run 或对话。"
        return self._run_status_text(run)

    async def _task_query_reply(
        self, account: str | None, sender: str | None, task_id: str
    ) -> str:
        if not account or not sender:
            return "未找到对应的任务。"
        try:
            task = await self.client.inspect(task_id)
        except (httpx.HTTPStatusError, ValueError):
            return "未找到对应的任务。"
        if (
            task.get("delivery_recipient") != sender
            or task.get("delivery_account") != account
        ):
            return "未找到对应的任务。"
        return self.legacy._technical_reply(task)

    @staticmethod
    def _run_status_text(run: dict[str, Any]) -> str:
        status = str(run.get("status", "unknown"))
        result = run.get("result")
        body = result.strip() if isinstance(result, str) and result.strip() else ""
        if status == "completed":
            detail = body or "本轮没有生成可显示回复。"
        elif status == "paused":
            detail = "任务已暂停。"
        elif status == "cancelled":
            detail = "任务已取消。"
        elif status == "recovery_required":
            detail = "本轮执行需要恢复核对, 请发送 /status 查询。"
        elif status == "failed":
            detail = "本轮执行失败, 请发送 /status 查询。"
        else:
            detail = body or f"任务状态: {status}"
        return f"Run {run.get('run_id', '')}\n状态: {status}\n{detail}"

    async def _wait_run(self, run_id: str) -> dict[str, Any]:
        cursor = 0
        while True:
            page = await self.client.run_events(run_id, after=cursor, wait=True)
            cursor = int(page.get("cursor", cursor))
            run = await self.client.get_run(run_id)
            final_cursor = int(run.get("cursor", cursor))
            if (
                run.get("status") not in {"queued", "running"}
                and cursor >= final_cursor
            ):
                return run

    @staticmethod
    def _final_text(run: dict[str, Any]) -> str:
        result = run.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
        status = str(run.get("status", "unknown"))
        if status == "completed":
            return "本轮没有生成可显示回复。"
        if status == "paused":
            return "任务已暂停。"
        if status == "recovery_required":
            return "本轮执行需要恢复核对, 请发送 /status 查询。"
        if status == "failed":
            return "本轮执行失败, 请发送 /status 查询。"
        return f"任务状态: {status}"


class WechatRuntimeAdapter:
    """Short user-facing replies over the shared Runtime API.

    Ordinary chat waits for the Run/task to settle and returns only its final
    content.  Technical IDs, worktrees and usage are reserved for explicit
    commands such as ``/status`` and ``/recovery``.
    """

    def __init__(self, client: RuntimeClient) -> None:
        self.client = client

    async def _wait(self, task_id: str) -> dict[str, object]:
        cursor = 0
        while True:
            page = await self.client.request(
                "GET", f"tasks/{task_id}/events?after={cursor}&wait=1"
            )
            cursor = int(page.get("cursor", cursor))
            task = await self.client.inspect(task_id)
            if task.get("status") not in {"queued", "running"}:
                return task

    @staticmethod
    def _result(task: dict[str, object]) -> str:
        value = task.get("result") or task.get("diagnostic") or ""
        return str(value).strip()

    @classmethod
    def _development_reply(cls, task: dict[str, object]) -> str:
        status = str(task.get("status", "unknown"))
        ident = str(task.get("task_id", ""))
        if status == "completed":
            body = cls._result(task)
            detail = f"开发任务已完成\n{body}" if body else "开发任务已完成。"
        elif status == "paused":
            detail = cls._result(task) or "开发任务已暂停。"
        elif status == "recovery_required":
            detail = "开发任务需要恢复核对, 请发送 /status 查询。"
        elif status == "failed":
            detail = "开发任务执行失败, 请发送 /status 查询。"
        else:
            detail = cls._result(task) or f"开发任务状态: {status}"
        return f"{ident}\n状态: {status}\n{detail}" if ident else detail

    @staticmethod
    def _technical_reply(task: dict[str, object]) -> str:
        raw_usage = task.get("metrics")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        raw_live = usage.get("live")
        live: dict[str, Any] = raw_live if isinstance(raw_live, dict) else {}
        steps = int(usage.get("steps", 0) or 0) + int(live.get("steps", 0) or 0)
        tokens = usage.get("total_tokens")
        progress = (
            f"步骤: {steps} · Token: {tokens if tokens is not None else '未知'} · "
            f"耗时: {usage.get('elapsed_seconds', '待统计')}s"
        )
        return (
            progress
            + "\n"
            + f"{task['task_id']}\n状态: {task['status']}\n"
            + f"Worktree: {task.get('worktree') or '准备中'}\n"
            + str(task.get("result") or task.get("diagnostic") or "")
        )

    async def reply(
        self,
        text: str,
        *,
        message_id: str | None = None,
        conversation_key: str | None = None,
        sender: str | None = None,
        account: str | None = None,
        deliver: bool = True,
        delivery_key: str | None = None,
        request_id: str | None = None,
    ) -> str:
        del delivery_key, request_id
        command, _, argument = text.strip().partition(" ")
        if command in {"/schedule", "/memory", "/help"}:
            result = await self.client.request(
                "POST",
                "commands",
                {
                    "command": command,
                    "args": argument,
                    "source": "wechat",
                    "request_id": message_id or "wechat-control:" + uuid4().hex,
                    "sender": sender,
                    "account": account,
                },
            )
            return str(result["message"])
        if command == "/reply":
            ident, _, prompt = argument.partition(" ")
            if not prompt.strip():
                return "用法: /reply TASK_ID 内容"
            task = await self.client.inspect(ident)
            task = await self.client.message(
                ident,
                prompt,
                source="wechat",
                request_id=message_id or "reply-" + str(task["sequence"]),
                expected_sequence=task["sequence"],
                delivery_recipient=sender,
                delivery_account=account,
            )
            return f"{task['task_id']}\n状态: {task['status']}"
        if command == "/history" and argument:
            RuntimeClient._id(argument.strip())
            messages = await self.client.request(
                "GET", f"tasks/{argument.strip()}/history"
            )
            return (
                argument.strip()
                + "\n最近已提交历史:\n"
                + "\n".join(
                    f"{m['role']}: {(m.get('content') or '')[:400]}"
                    for m in messages[-6:]
                )
            )
        if command == "/recovery" and argument:
            RuntimeClient._id(argument.strip())
            return json.dumps(
                await self.client.request("GET", f"tasks/{argument.strip()}/recovery"),
                ensure_ascii=False,
            )
        if command in {"/status", "/task"} and argument.startswith("task-"):
            task = await self.client.inspect(argument.strip())
            return self._technical_reply(task)
        if command in {"/cancel", "/continue"} and argument:
            task = await self.client.control(argument.strip(), command[1:])
            return self._technical_reply(task)
        if command == "/status":
            tasks = await self.client.request("GET", "tasks")
            return (
                "\n".join(f"{t['task_id']} {t['status']}" for t in tasks[-10:])
                or "暂无任务"
            )
        development = command == "/task"
        if not development:
            raise ValueError("普通微信聊天必须使用 WechatConversationAdapter")
        try:
            task = await self.client.submit(
                argument if development else text,
                "wechat",
                request_id=message_id,
                mode="development" if development else "conversation",
                conversation_key=None if development else conversation_key,
                delivery_recipient=sender,
                delivery_account=account,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409 or conversation_key is None:
                raise
            tasks = await self.client.request("GET", "tasks")
            busy_task = next(
                (
                    item
                    for item in reversed(tasks)
                    if item.get("conversation_key") == conversation_key
                ),
                None,
            )
            if busy_task is None:
                raise
            return "上一条消息仍在处理中, 请稍后重发。"
        if not deliver:
            # ServerControls is the single delivery owner for adapter-forwarded
            # tasks and will send the final result from TaskService.on_settled.
            return ""
        task = await self._wait(str(task["task_id"]))
        if development:
            return self._development_reply(task)
        raise ValueError("普通微信聊天必须使用 WechatConversationAdapter")
