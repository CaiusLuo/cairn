"""Thin terminal presentation of Server-owned controls."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from cairn.infra.runtime_client import RuntimeClient
from cairn.integrations.wechat.qr import render_ascii_qr


class ServerCommands:
    def __init__(self, api: RuntimeClient) -> None:
        self.api = api
        self.proposal: str | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.last_wechat: tuple[object, ...] | None = None

    async def execute(
        self, command: str, args: str, task_id: str | None
    ) -> dict[str, Any]:
        signature = repr((command, args, task_id))
        if signature not in self.pending:
            self.pending[signature] = {
                "command": command,
                "args": args,
                "task_id": task_id,
                "source": "cli",
                "request_id": "cli-control:" + uuid4().hex,
                "confirmed": command == "/management"
                or (command == "/memory" and args.strip() == "approve"),
                "expected_proposal": self.proposal,
            }
        payload = self.pending[signature]
        result = dict(await self.api.request("POST", "commands", payload))
        del self.pending[signature]
        print(result["message"])
        if command == "/memory" and args.strip() in {"pending", "review"}:
            self.proposal = result.get("proposal_digest")
        if command == "/wx":
            await self.show_wechat()
        return result

    async def show_wechat(self) -> None:
        data = await self.api.request("GET", "management?section=wechat")
        state = data["wechat"]
        key = (state["status"], state.get("qr_content"))
        if key == self.last_wechat:
            return
        self.last_wechat = key
        print(state["status"])
        if state.get("qr_content"):
            try:
                print(render_ascii_qr(state["qr_content"]))
            except ValueError:
                print("终端太窄, 请在 Web → 管理 → 微信 扫码。")
        if state["verification_required"]:
            print("输入 /wx verify 验证码 完成验证。")

    async def watch_wechat(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self.last_wechat is not None:
                try:
                    await self.show_wechat()
                except Exception:
                    # Status polling must not interrupt terminal input or execution.
                    continue
