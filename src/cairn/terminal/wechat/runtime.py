"""Wechat text adapter over the same authenticated Runtime HTTP API."""

from __future__ import annotations

from cairn.infra.runtime_client import RuntimeClient


class WechatRuntimeAdapter:
    def __init__(self, client: RuntimeClient) -> None:
        self.client = client

    async def reply(self, text: str, *, message_id: str | None = None) -> str:
        command, _, argument = text.strip().partition(" ")
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
            import json

            RuntimeClient._id(argument.strip())
            return json.dumps(
                await self.client.request("GET", f"tasks/{argument.strip()}/recovery"),
                ensure_ascii=False,
            )
        if command in {"/status", "/task"} and argument.startswith("task-"):
            task = await self.client.inspect(argument.strip())
        elif command in {"/cancel", "/continue"} and argument:
            task = await self.client.control(argument.strip(), command[1:])
        elif command == "/status":
            tasks = await self.client.request("GET", "tasks")
            return (
                "\n".join(f"{t['task_id']} {t['status']}" for t in tasks[-10:])
                or "暂无任务"
            )
        else:
            task = await self.client.submit(
                argument if command == "/task" else text,
                "wechat",
                request_id=message_id,
            )
        usage = task.get("metrics", {})
        live = usage.get("live") or {}
        steps = usage.get("steps", 0) + live.get("steps", 0)
        tokens = usage.get("total_tokens")
        progress = f"步骤: {steps} · Token: {tokens if tokens is not None else '未知'} · 耗时: {usage.get('elapsed_seconds', '待统计')}s"
        return (
            progress
            + "\n"
            + f"{task['task_id']}\n状态: {task['status']}\nWorktree: {task.get('worktree') or '准备中'}\n{task.get('result') or task.get('diagnostic') or ''}"
        )
