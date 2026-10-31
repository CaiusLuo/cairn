"""Scheduled-task command and controller regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cairn.infra.schedule.store import ScheduleStore
from cairn.terminal.headless import HeadlessResult, HeadlessStatus
from cairn.terminal.schedule.controller import ScheduleController

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _controller(tmp_path: Path) -> ScheduleController:
    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(tmp_path),
        )

    async def deliver(sender: str, text: str) -> bool:
        return True

    return ScheduleController(
        workspace=tmp_path,
        store=ScheduleStore(tmp_path / "schedule"),
        executor=executor,
        deliver=deliver,
        recipient=lambda: "owner",
        account_id=lambda: "bot-test",
        clock=lambda: NOW,
    )


def test_schedule_command_add_list_pause_resume_remove(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        added = await controller.command(
            "add once 2026-11-01T10:00:00+08:00 write report"
        )
        assert "已创建" in getattr(added, "message", "")
        jobs = controller.store.load_jobs()
        assert len(jobs) == 1
        job_id = jobs[0].job_id

        listed = await controller.command("list")
        assert job_id in getattr(listed, "message", "")

        paused = await controller.command(f"pause {job_id}")
        assert "暂停" in getattr(paused, "message", "")
        assert controller.store.load_jobs()[0].enabled is False

        resumed = await controller.command(f"resume {job_id}")
        assert "恢复" in getattr(resumed, "message", "")
        assert controller.store.load_jobs()[0].enabled is True

        removed = await controller.command(f"remove {job_id}")
        assert "删除" in getattr(removed, "message", "")
        assert controller.store.load_jobs() == ()

    asyncio.run(scenario())


def test_schedule_add_rejects_invalid_args_without_writing(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        action = await controller.command("add daily 25:00 bad")
        assert "无效" in getattr(action, "message", "")
        assert controller.store.load_jobs() == ()

    asyncio.run(scenario())


def test_schedule_list_and_history_show_last_run(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    async def scenario() -> None:
        await controller.command("add once 2026-11-01T10:00:00+08:00 write report")
        job_id = controller.store.load_jobs()[0].job_id
        controller.store.append_run(
            {
                "run_key": f"{job_id}:2026-11-01T10:00:00+08:00",
                "job_id": job_id,
                "workspace": str(controller.workspace.resolve()),
                "recipient": "owner",
                "account_id": "bot-test",
                "status": "completed",
                "finished_at": "2026-11-01T10:00:02+08:00",
                "task_id": "scheduled-1",
                "trace_id": "trace-1",
                "delivered": True,
                "result": "UNIQUE-SCHEDULE-RESULT",
            }
        )
        jobs_now = controller.store.load_jobs()
        controller.store.save_jobs(
            tuple(
                job.model_copy(update={"last_status": "completed", "last_run_at": NOW})
                if job.job_id == job_id
                else job
                for job in jobs_now
            )
        )
        listed = await controller.command("list")
        assert f"{job_id}" in getattr(listed, "message", "")
        assert "last=completed" in getattr(listed, "message", "")
        assert "at=2026-11-01T09:00:00" in getattr(listed, "message", "")
        history = await controller.command("history")
        assert "completed" in getattr(history, "message", "")
        assert "scheduled-1" in getattr(history, "message", "")
        assert "UNIQUE-SCHEDULE-RESULT" in getattr(history, "message", "")

    asyncio.run(scenario())
