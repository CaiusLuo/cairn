"""Real CLI loop + fake model: local turn and due scheduled task must serialize."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

import cairn.infra.llm.litellm_client as llm_module
import cairn.terminal.cli as cli_module
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.scheduling.models import ScheduleJob, ScheduleKind
from cairn.scheduling.store import ScheduleStore
from tests.support.cli_harness import completion_response, configure_cli

NOW = datetime.now(ZoneInfo("Asia/Shanghai"))


def test_real_cli_local_turn_and_due_schedule_share_one_task_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path.parent / (tmp_path.name + "-wechat")
    WechatState(state_dir).save_account(
        WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    schedule_dir = tmp_path.parent / (tmp_path.name + "-schedule")
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(schedule_dir))
    monkeypatch.setattr(cli_module, "default_state_dir", lambda: state_dir)
    ScheduleStore(schedule_dir).save_jobs(
        (
            ScheduleJob(
                job_id="job-cli",
                prompt="scheduled question",
                workspace=str(tmp_path),
                recipient="owner",
                account_id="bot-test",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )
    _state = configure_cli(tmp_path, monkeypatch, "local question")

    class DelayedExit:
        def __init__(self) -> None:
            self._sent = False

        async def read(self) -> str:
            if not self._sent:
                self._sent = True
                return "local question"
            await asyncio.sleep(0.8)
            return "/exit"

    monkeypatch.setattr(cli_module, "CliInput", DelayedExit)
    monkeypatch.delenv("CAIRN_AGENT_MEMORY", raising=False)
    active = 0
    maximum = 0
    calls = 0

    async def completion(**_kwargs: object) -> object:
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.2)
            return completion_response("ok")
        finally:
            active -= 1

    monkeypatch.setattr(llm_module, "acompletion", completion)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    assert maximum == 1
    assert calls >= 2


def test_cli_schedule_and_wechat_keep_distinct_task_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from cairn.development.board.models import TaskSource
    from cairn.development.board.store import TaskBoardStore
    from cairn.development.execution.headless import HeadlessResult, HeadlessStatus
    from cairn.integrations.wechat.session import WechatSessionController
    from cairn.scheduling.commands import ScheduleController

    board_dir = tmp_path.parent / (tmp_path.name + "-board")
    state_dir = tmp_path.parent / (tmp_path.name + "-wechat-origin")
    schedule_dir = tmp_path.parent / (tmp_path.name + "-schedule-origin")
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(board_dir))
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(schedule_dir))
    monkeypatch.setattr(cli_module, "default_state_dir", lambda: state_dir)
    configure_cli(tmp_path, monkeypatch)
    executors: list[Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]] = []
    original_controller = WechatSessionController

    def capture_wechat(**kwargs: Any) -> Any:
        executors.append(kwargs["task_executor"])
        return original_controller(**kwargs)

    async def fake_headless(**kwargs: Any) -> HeadlessResult:
        tool = next(
            tool for tool in kwargs["extra_tools"] if tool.name == "task_create"
        )
        created = await tool.execute({"title": kwargs["prompt"]})
        assert created.exit_code == 0
        return HeadlessResult(
            task_id=kwargs["task_id"],
            status=HeadlessStatus.COMPLETED,
            workspace=str(tmp_path),
        )

    async def run_both(controller: ScheduleController) -> None:
        await controller.runner.executor(
            "scheduled task", asyncio.Event(), "scheduled-job-cli"
        )
        await executors[0]("wechat task", asyncio.Event(), "wechat-msg")

    monkeypatch.setattr(cli_module, "WechatSessionController", capture_wechat)
    monkeypatch.setattr(cli_module, "run_headless_task", fake_headless)
    monkeypatch.setattr(ScheduleController, "start", run_both)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    tasks = {task.title: task for task in TaskBoardStore(board_dir).load()}
    assert tasks["scheduled task"].source is TaskSource.SCHEDULE
    assert tasks["scheduled task"].session_id == "schedule:scheduled-job-cli"
    assert tasks["wechat task"].source is TaskSource.WECHAT
    assert tasks["wechat task"].session_id.startswith("wechat:")
