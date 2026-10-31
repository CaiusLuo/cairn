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
from cairn.core.schedule import ScheduleJob, ScheduleKind
from cairn.infra.schedule.store import ScheduleStore
from cairn.infra.wechat.state import WechatAccount, WechatState
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
