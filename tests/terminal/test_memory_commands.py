"""`/memory` command behavior: note, proposal approval/rejection, rollback."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.terminal.memory.controller import MemoryController

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _controller(tmp_path: Path) -> MemoryController:
    service = PersonalMemoryService(
        AgentMemoryStore(tmp_path / "agent"), clock=lambda: NOW
    )
    return MemoryController(service)


def test_memory_note_show_and_proposal_flow(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        added = await controller.command("note 我喜欢简洁的回答")
        assert "已记录" in getattr(added, "message", "")
        shown = await controller.command("show")
        assert "每日记忆文件: 1 天" in getattr(shown, "message", "")

        proposed = await controller.command("propose Prefer short answers.")
        assert "提案" in getattr(proposed, "message", "")
        pending = await controller.command("approve")
        assert "已应用" in getattr(pending, "message", "")
        assert "Prefer short answers." in controller.service.store.read_agent_md()

        rolled = await controller.command("rollback")
        assert "已回滚" in getattr(rolled, "message", "")

    asyncio.run(scenario())


def test_memory_reject_clears_pending(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        await controller.command("propose do not apply")
        rejected = await controller.command("reject")
        assert "已放弃" in getattr(rejected, "message", "")
        assert controller.service.pending_proposal() is None

    asyncio.run(scenario())


def test_memory_controller_notifies_agent_after_approve_and_rollback(
    tmp_path: Path,
) -> None:
    service = PersonalMemoryService(
        AgentMemoryStore(tmp_path / "agent"), clock=lambda: NOW
    )
    changed: list[str] = []
    controller = MemoryController(
        service, on_agent_changed=lambda: changed.append("yes")
    )

    async def scenario() -> None:
        await controller.command("propose learned preference")
        await controller.command("approve")
        assert changed == ["yes"]
        await controller.command("rollback")
        assert changed == ["yes", "yes"]

    asyncio.run(scenario())


def test_memory_propose_auto_uses_daily_candidates(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        await controller.command("note 优先简洁")
        generated = await controller.command("propose-auto")
        assert "已根据每日记忆" in getattr(generated, "message", "")
        pending = controller.service.pending_proposal()
        assert pending is not None
        assert "优先简洁" in str(pending.get("text"))

    asyncio.run(scenario())
