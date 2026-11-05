"""The public CLI installs the task-selection capability for its current project."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairn.runtime.task_board.tools import TaskUseTool
from cairn.terminal.cli import app
from tests.support.cli_harness import configure_cli


def test_cli_registers_task_selection_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "CAIRN_TASK_BOARD_DIR", str(tmp_path.parent / (tmp_path.name + "-board"))
    )
    state = configure_cli(tmp_path, monkeypatch, "/exit")
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert state.agents
    tool = state.agents[0].tools.get_tool("task_use")
    assert isinstance(tool, TaskUseTool)
    assert tool.service.project == str(tmp_path.resolve())
    assert tool.service.session_id
