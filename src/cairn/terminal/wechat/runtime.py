"""Wechat text adapter over the same authenticated Runtime HTTP API."""

from __future__ import annotations

from cairn.infra.runtime_client import RuntimeClient


class WechatRuntimeAdapter:
    def __init__(self, client: RuntimeClient) -> None:
        self.client = client

    async def reply(self, text: str, *, message_id: str | None = None) -> str:
        command, _, argument = text.strip().partition(" ")
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
        return f"{task['task_id']}\n状态: {task['status']}\nWorktree: {task.get('worktree') or '准备中'}\n{task.get('result') or task.get('diagnostic') or ''}"
