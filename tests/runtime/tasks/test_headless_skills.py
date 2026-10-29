"""Server task entry reuses the project skill catalog and --skill selection."""

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse
from cairn.infra.skills import layered_skills
from cairn.terminal.headless import HeadlessStatus, run_headless_task, task_app
from tests.support.runtime import SequenceLLM

BODY = "PROJECT-SKILL-CONTEXT-MARKER"


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    target = ws / ".cairn" / "skills" / "local"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: local\ndescription: Local helper\n---\n\n{BODY}\n",
        encoding="utf-8",
    )
    return ws


def test_headless_skill_selection_reaches_model(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM([LLMResponse(content="done")])
    result = asyncio.run(
        run_headless_task(
            workspace=ws,
            prompt="task",
            grant_workspace_trust=True,
            grant_shell=False,
            llm=llm,
            budget=RunBudget(max_steps=3),
            skills=layered_skills(ws),
            selected_skills=["local"],
        )
    )
    assert result.status is HeadlessStatus.COMPLETED
    users = "\n".join(m.content or "" for m in llm.calls[0][0] if m.role == "user")
    assert BODY in users


def test_headless_unknown_skill_is_invalid_before_running(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM([LLMResponse(content="should not run")])
    result = asyncio.run(
        run_headless_task(
            workspace=ws,
            prompt="task",
            grant_workspace_trust=True,
            grant_shell=False,
            llm=llm,
            budget=RunBudget(max_steps=3),
            skills=layered_skills(ws),
            selected_skills=["nope"],
        )
    )
    assert result.status is HeadlessStatus.INVALID_REQUEST
    assert result.error_category == "unknown_skill"
    assert llm.calls == []


def test_cli_unknown_skill_is_rejected_offline(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--skill",
            "nope",
            "--json",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_request"
    assert payload["error_category"] == "unknown_skill"
