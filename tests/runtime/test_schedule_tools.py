from __future__ import annotations

import asyncio
from pathlib import Path

from cairn.infra.schedule.store import ScheduleStore
from cairn.runtime.scheduling.tools import (
    ScheduleCreateTool,
    ScheduleListTool,
    ScheduleManageTool,
)


def _tool(tmp_path: Path) -> ScheduleCreateTool:
    return ScheduleCreateTool(
        store=ScheduleStore(tmp_path / "schedule"),
        workspace=str(tmp_path / "ws"),
        recipient=lambda: "owner",
        account_id=lambda: "bot",
    )


def test_schedule_create_returns_brief_before_confirmation(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = asyncio.run(
        tool.execute({"prompt": "check", "kind": "interval", "when": "3600"})
    )
    assert result.exit_code == 0
    assert "Schedule brief" in result.stdout
    assert "confirm" in result.stdout.lower()
    assert tool.store.load_jobs() == ()


def test_schedule_create_and_manage_owner_scope(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    result = asyncio.run(
        tool.execute(
            {
                "prompt": "check",
                "kind": "interval",
                "when": "3600",
                "confirmed": True,
            }
        )
    )
    assert result.exit_code == 0
    jobs = tool.store.load_jobs()
    assert len(jobs) == 1 and jobs[0].recipient == "owner"
    job_id = jobs[0].job_id

    listed = asyncio.run(
        ScheduleListTool(
            store=tool.store,
            workspace=tool.workspace,
            recipient=lambda: "owner",
            account_id=lambda: "bot",
        ).execute({})
    )
    assert job_id in listed.stdout

    managed = asyncio.run(
        ScheduleManageTool(
            store=tool.store,
            workspace=tool.workspace,
            recipient=lambda: "owner",
            account_id=lambda: "bot",
        ).execute({"job_id": job_id, "action": "remove"})
    )
    assert managed.exit_code == 0
    assert tool.store.load_jobs() == ()
