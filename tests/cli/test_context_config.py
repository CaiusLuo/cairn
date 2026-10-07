import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cairn.cli as cli_module
from cairn.assembly import build_agent
from cairn.core.agent import Agent
from cairn.core.context import ContextBudget
from cairn.llm.litellm_client import LiteLLMClient
from cairn.llm.token_counter import LiteLLMTokenCounter
from cairn.terminal.input import CliInput

CONTEXT_SETTINGS = ("CAIRN_CONTEXT_MAX_TOKENS", "CAIRN_RESPONSE_MAX_TOKENS")


class ExitInput(CliInput):
    async def read(self) -> str:
        return "/quit"


def _configure_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_values: dict[str, str],
    file_values: dict[str, str],
) -> list[Agent]:
    monkeypatch.chdir(tmp_path)
    for name in CONTEXT_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "CAIRN_LLM_MODEL": "provider/model",
        "CAIRN_LLM_API_KEY": "test-key",
        "CAIRN_BASE_URL": "https://example.test/v1",
        **host_values,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: file_values)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    monkeypatch.setattr(cli_module, "CliInput", ExitInput)
    agents: list[Agent] = []

    def capture_agent(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        agents.append(agent)
        return agent

    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    return agents


@pytest.mark.parametrize(
    ("host_values", "file_values", "expected"),
    [
        ({}, {}, ContextBudget()),
        (
            {},
            {"CAIRN_CONTEXT_MAX_TOKENS": "2048", "CAIRN_RESPONSE_MAX_TOKENS": "256"},
            ContextBudget(max_tokens=2048, response_tokens=256),
        ),
        (
            {"CAIRN_CONTEXT_MAX_TOKENS": "3072", "CAIRN_RESPONSE_MAX_TOKENS": "512"},
            {"CAIRN_CONTEXT_MAX_TOKENS": "2048", "CAIRN_RESPONSE_MAX_TOKENS": "256"},
            ContextBudget(max_tokens=3072, response_tokens=512),
        ),
    ],
)
def test_cli_injects_context_budget_and_matching_provider_response_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_values: dict[str, str],
    file_values: dict[str, str],
    expected: ContextBudget,
) -> None:
    agents = _configure_cli(tmp_path, monkeypatch, host_values, file_values)

    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert len(agents) == 1
    assert agents[0].context_builder.budget == expected
    assert isinstance(agents[0].context_builder.counter, LiteLLMTokenCounter)
    assert agents[0].context_builder.counter.model == "provider/model"
    assert isinstance(agents[0].llm, LiteLLMClient)
    assert agents[0].llm.max_output_tokens == expected.response_tokens
    for name in CONTEXT_SETTINGS:
        assert os.environ.get(name) == host_values.get(name)


@pytest.mark.parametrize(
    "values",
    [
        {"CAIRN_CONTEXT_MAX_TOKENS": "many"},
        {"CAIRN_CONTEXT_MAX_TOKENS": "0"},
        {"CAIRN_RESPONSE_MAX_TOKENS": "1.5"},
        {"CAIRN_RESPONSE_MAX_TOKENS": "0"},
        {"CAIRN_CONTEXT_MAX_TOKENS": "1024", "CAIRN_RESPONSE_MAX_TOKENS": "1024"},
    ],
)
def test_cli_rejects_invalid_context_configuration_before_agent_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
) -> None:
    agents = _configure_cli(tmp_path, monkeypatch, values, {})

    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert agents == []


@pytest.mark.parametrize(
    ("host_values", "file_values"),
    [
        ({"CAIRN_CONTEXT_MAX_TOKENS": ""}, {}),
        ({"CAIRN_RESPONSE_MAX_TOKENS": ""}, {}),
        ({}, {"CAIRN_CONTEXT_MAX_TOKENS": "", "CAIRN_RESPONSE_MAX_TOKENS": ""}),
    ],
)
def test_cli_treats_blank_context_settings_as_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_values: dict[str, str],
    file_values: dict[str, str],
) -> None:
    agents = _configure_cli(tmp_path, monkeypatch, host_values, file_values)

    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert agents[0].context_builder.budget == ContextBudget()
