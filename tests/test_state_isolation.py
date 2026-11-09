from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.infra.schedule.store import default_schedule_dir
from cairn.infra.wechat.state import default_state_dir
from cairn.memory.store import default_agent_home


def test_host_private_state_is_isolated_per_test() -> None:
    root = Path(os.environ["CAIRN_AGENT_HOME"]).parent
    assert default_agent_home().is_relative_to(root)
    assert default_schedule_dir().is_relative_to(root)
    # WeChat's current public path follows XDG_STATE_HOME; the fixture sets it.
    assert default_state_dir().is_relative_to(Path(os.environ["XDG_STATE_HOME"]))


def test_explicit_state_overrides_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = tmp_path / "explicit-schedule"
    agent = tmp_path / "explicit-agent"
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(schedule))
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(agent))
    assert default_schedule_dir() == schedule
    assert default_agent_home() == agent


def test_subprocess_inherits_isolated_state_paths() -> None:
    code = (
        "import os;"
        "print(os.environ['CAIRN_AGENT_HOME']);"
        "print(os.environ['CAIRN_SCHEDULE_DIR']);"
        "print(os.environ['XDG_STATE_HOME'])"
    )
    output = subprocess.check_output([sys.executable, "-c", code], text=True)
    paths = output.splitlines()
    assert paths[0] == os.environ["CAIRN_AGENT_HOME"]
    assert paths[1] == os.environ["CAIRN_SCHEDULE_DIR"]
    assert paths[2] == os.environ["XDG_STATE_HOME"]
