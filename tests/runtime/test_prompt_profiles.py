"""Entry points select one prompt without changing execution authority."""

import asyncio
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from cairn.runtime.prompts import BASE_SYSTEM_PROMPT, system_prompt_for
from cairn.terminal.headless import HeadlessStatus, run_headless_task
from tests.support.runtime import SequenceLLM


def test_general_agent_uses_only_base_rules(tmp_path: Path) -> None:
    llm = SequenceLLM([LLMResponse(content="Hello")])
    agent = build_agent(
        workspace=Workspace(tmp_path),
        llm=llm,
        permission_handler=None,
        event_handler=None,
        tracer=None,
        summarize=False,
    )
    asyncio.run(run_turn(agent, "Hello", budget=RunBudget(max_steps=1)))
    assert llm.calls[0][0][0].content == BASE_SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, system_prompt_for("development")),
        (system_prompt_for("chat"), system_prompt_for("chat")),
        (system_prompt_for("classification"), system_prompt_for("classification")),
        ("Host-specific contract", "Host-specific contract"),
    ],
)
def test_headless_request_uses_selected_profile(
    tmp_path: Path, override: str | None, expected: str
) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    result = asyncio.run(
        run_headless_task(
            workspace=tmp_path,
            prompt="Describe the current state",
            grant_workspace_trust=True,
            grant_shell=False,
            llm=llm,
            budget=RunBudget(max_steps=1),
            system_prompt=override,
        )
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert llm.calls[0][0][0].content == expected
