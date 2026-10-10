import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from examples.evals import run_coding_smoke as script

import cairn.llm.litellm_client as llm_module
import cairn.llm.token_counter as counter_module
import cairn.terminal.output as ui_module
from cairn.assembly import build_agent
from cairn.config import resolve_provider_api_key
from cairn.core.agent import Agent
from cairn.evals import (
    EvalCase,
    EvalSuite,
    EvalSuiteCase,
    EvalSuiteResult,
    FileExistsCheck,
)
from cairn.evals import runner as runner_module

FIRST_PROVIDER = """\
[[providers]]
name = "first"
base_url = "https://first.test/v1"
api_key_env = "FIRST_API_KEY"

[[providers.models]]
name = "primary"
model_ids = ["openai/first", "openai/first-backup"]

[[providers.models]]
name = "another"
model_ids = ["openai/another"]
"""

SECOND_PROVIDER = """\
[[providers]]
name = "second"
base_url = "https://second.test/v1"
api_key_env = "SECOND_API_KEY"

[[providers.models]]
name = "secondary"
model_ids = ["openai/second"]
"""

LEGACY_TOML = """\
base_url = "https://first.test/v1"
api_key_env = "FIRST_API_KEY"

[[models]]
name = "primary"
model_ids = ["openai/first", "openai/first-backup"]
"""

ENV_VALUES = {
    "CAIRN_LLM_MODEL": "openai/old-env-model",
    "CAIRN_LLM_API_KEY": "FAKE_ENV_CREDENTIAL",
    "CAIRN_BASE_URL": "https://env.test/v1",
    "FIRST_API_KEY": "FAKE_FIRST_CREDENTIAL",
    "SECOND_API_KEY": "FAKE_SECOND_CREDENTIAL",
    "CAIRN_CONTEXT_MAX_TOKENS": "100",
    "CAIRN_RESPONSE_MAX_TOKENS": "20",
}


def prepare_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str | None
) -> SimpleNamespace:
    monkeypatch.chdir(tmp_path)
    if toml is not None:
        path = tmp_path / ".cairn/models.toml"
        path.parent.mkdir()
        path.write_text(toml)
    for name in ENV_VALUES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(script, "dotenv_values", lambda: dict(ENV_VALUES))
    state = SimpleNamespace(approvals=[], resolved_keys=[], agents=[], requests=[])

    def approve(provider: Any, group: Any, *, switching: bool) -> bool:
        state.approvals.append((provider.name, group.name, switching))
        return True

    def resolve_key(config: Any, host_env: Any, file_values: Any) -> str:
        state.resolved_keys.append(config.api_key_env)
        return resolve_provider_api_key(config, host_env, file_values)

    def capture_agent(**kwargs: Any) -> Agent:
        state.secret_env_keys = kwargs["secret_env_keys"]
        agent = build_agent(**kwargs)
        state.agents.append(agent)
        return agent

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))
            ],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=2),
        )

    monkeypatch.setattr(script, "confirm_provider_access", approve)
    monkeypatch.setattr(script, "resolve_provider_api_key", resolve_key)
    monkeypatch.setattr(runner_module, "build_agent", capture_agent)
    monkeypatch.setattr(llm_module, "acompletion", completion)
    monkeypatch.setattr(counter_module, "token_counter", lambda **_: 10)
    selection = Mock(side_effect=AssertionError("Startup must not ask for selection"))
    monkeypatch.setattr(ui_module, "choose_provider", selection)
    monkeypatch.setattr(ui_module, "choose_model_group", selection)
    state.selection = selection
    monkeypatch.setattr(
        script,
        "coding_smoke_suite",
        lambda: EvalSuite(
            "smoke",
            (
                EvalSuiteCase(
                    EvalCase(name="one", prompt="inspect", files={"answer.txt": "ok"}),
                    (FileExistsCheck("answer.txt"),),
                ),
            ),
        ),
    )
    return state


@pytest.mark.parametrize(
    ("toml", "provider", "model", "key", "endpoint", "secret_keys"),
    [
        (
            None,
            "default",
            "openai/old-env-model",
            "CAIRN_LLM_API_KEY",
            "https://env.test/v1",
            frozenset({"CAIRN_LLM_API_KEY"}),
        ),
        (
            LEGACY_TOML,
            "default",
            "openai/first",
            "FIRST_API_KEY",
            "https://first.test/v1",
            frozenset({"FIRST_API_KEY"}),
        ),
        (
            FIRST_PROVIDER + SECOND_PROVIDER,
            "first",
            "openai/first",
            "FIRST_API_KEY",
            "https://first.test/v1",
            frozenset({"FIRST_API_KEY", "SECOND_API_KEY"}),
        ),
        (
            SECOND_PROVIDER + FIRST_PROVIDER,
            "second",
            "openai/second",
            "SECOND_API_KEY",
            "https://second.test/v1",
            frozenset({"FIRST_API_KEY", "SECOND_API_KEY"}),
        ),
    ],
)
def test_smoke_uses_first_configured_provider_group_and_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    toml: str | None,
    provider: str,
    model: str,
    key: str,
    endpoint: str,
    secret_keys: frozenset[str],
) -> None:
    state = prepare_script(tmp_path, monkeypatch, toml)
    path = tmp_path / "report.json"
    assert asyncio.run(script.main(path)) == 0
    assert len(state.requests) == 1
    assert state.requests[0]["model"] == model
    assert state.requests[0]["api_key"] == ENV_VALUES[key]
    assert state.requests[0]["api_base"] == endpoint
    assert state.requests[0]["max_tokens"] == 20
    assert state.resolved_keys == [key]
    assert state.secret_env_keys == secret_keys
    state.selection.assert_not_called()
    if toml is None:
        assert state.approvals == []
    else:
        assert len(state.approvals) == 1
        assert state.approvals[0][0] == provider
        assert state.approvals[0][2] is False
    report = EvalSuiteResult.model_validate_json(path.read_text())
    assert report.counts.passed == report.counts.completed == 1
    assert report.config.context_budget.max_tokens == 100
    assert report.config.context_budget.response_tokens == 20
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.model_identifier == model
    assert metrics.provider_max_output_tokens == 20
    assert metrics.request_token_counts_estimated is False
    output = capsys.readouterr().out
    assert f"Provider: '{provider}'" in output
    assert model in output
    assert "FAKE_" not in output + path.read_text()


def test_smoke_denied_provider_does_not_resolve_a_key_or_create_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = prepare_script(tmp_path, monkeypatch, FIRST_PROVIDER + SECOND_PROVIDER)
    monkeypatch.setattr(
        script, "confirm_provider_access", lambda *_args, **_kwargs: False
    )
    path = tmp_path / "report.json"
    assert asyncio.run(script.main(path)) == 2
    assert state.resolved_keys == []
    assert state.agents == [] and state.requests == []
    assert not path.exists()


@pytest.mark.parametrize("toml", ["", '[[providers]]\nname = "incomplete"'])
def test_smoke_invalid_toml_never_falls_back_to_old_environment_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
) -> None:
    state = prepare_script(tmp_path, monkeypatch, toml)
    path = tmp_path / "report.json"
    assert asyncio.run(script.main(path)) == 2
    assert state.approvals == [] and state.resolved_keys == []
    assert state.agents == [] and state.requests == []
    assert not path.exists()


def test_smoke_missing_first_provider_key_does_not_use_second_or_env_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = prepare_script(tmp_path, monkeypatch, FIRST_PROVIDER + SECOND_PROVIDER)
    monkeypatch.setattr(
        script,
        "dotenv_values",
        lambda: {
            key: value for key, value in ENV_VALUES.items() if key != "FIRST_API_KEY"
        },
    )
    path = tmp_path / "report.json"
    assert asyncio.run(script.main(path)) == 2
    assert state.resolved_keys == ["FIRST_API_KEY"]
    assert state.agents == [] and state.requests == []
    assert not path.exists()
