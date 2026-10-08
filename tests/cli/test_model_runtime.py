import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from typer.testing import CliRunner

import cairn.cli as cli_module
import cairn.llm.litellm_client as llm_module
import cairn.llm.token_counter as counter_module
from cairn.assembly import build_agent
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.core.loop import run_turn
from cairn.llm.model_manager import ProviderConfig
from cairn.workspace.workspace import Workspace
from tests.support.sandbox import require_working_sandbox

API_KEY = "test-only-provider-credential"
TOML = """\
base_url = "https://example.test/v1"
api_key_env = "BAILIAN_API_KEY"

[[models]]
name = "flash"
model_id = "openai/qwen-flash"

[[models]]
name = "plus"
model_id = "openai/qwen-plus"
"""


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))
        ]
    )


def _configure_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *commands: str,
    toml: str | None = TOML,
) -> tuple[list[Agent], list[dict[str, Any]], list[str]]:
    monkeypatch.chdir(tmp_path)
    if toml is not None:
        (tmp_path / ".cairn").mkdir()
        (tmp_path / ".cairn/models.toml").write_text(toml, encoding="utf-8")
    for name, value in {
        "BAILIAN_API_KEY": API_KEY,
        "CAIRN_LLM_MODEL": "openai/legacy-model",
        "CAIRN_LLM_API_KEY": "test-only-legacy-key",
        "CAIRN_BASE_URL": "https://legacy.test/v1",
        "CAIRN_CONTEXT_MAX_TOKENS": "100",
        "CAIRN_RESPONSE_MAX_TOKENS": "20",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: {})
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    monkeypatch.setattr(cli_module, "confirm_model_provider", lambda _: True)
    monkeypatch.setattr(
        cli_module,
        "CliInput",
        lambda: SimpleNamespace(read=AsyncMock(side_effect=[*commands, "/exit"])),
    )
    agents: list[Agent] = []
    requests: list[dict[str, Any]] = []
    token_models: list[str] = []

    def capture_agent(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        agents.append(agent)
        return agent

    async def completion(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return _response(f"reply {len(requests)}")

    def count(**kwargs: Any) -> int:
        token_models.append(kwargs["model"])
        return 30

    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    monkeypatch.setattr(llm_module, "acompletion", completion)
    monkeypatch.setattr(counter_module, "token_counter", count)
    return agents, requests, token_models


def test_toml_default_and_session_switch_use_real_loop_and_preserve_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, requests, token_models = _configure_cli(
        tmp_path,
        monkeypatch,
        "/model",
        "/model list",
        "first question",
        "/model use plus",
        "/model list",
        "second question",
    )
    identities: list[tuple[object, ...]] = []

    async def observe_turn(agent: Agent, user_input: str, *, budget: RunBudget) -> str:
        identities.append(
            (agent, agent.state, agent.tools, agent.permission_handler, agent.tracer)
        )
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", observe_turn)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0, result.output
    assert "Current model: flash (openai/qwen-flash)" in result.output
    assert "Current model: plus (openai/qwen-plus)" in result.output
    assert "* flash (openai/qwen-flash)" in result.output
    assert "* plus (openai/qwen-plus)" in result.output
    assert len(agents) == 1
    assert all(before is after for before, after in zip(*identities, strict=True))
    assert [request["model"] for request in requests] == [
        "openai/qwen-flash",
        "openai/qwen-plus",
    ]
    assert token_models == ["openai/qwen-flash", "openai/qwen-plus"]
    for request in requests:
        assert request["api_key"] == API_KEY
        assert request["api_base"] == "https://example.test/v1"
        assert request["max_tokens"] == 20
        assert API_KEY not in repr(request["messages"])
        assert API_KEY not in repr(request["tools"])
    assert [
        (m["role"], m["content"])
        for m in requests[1]["messages"]
        if m["role"] != "system"
    ] == [
        ("user", "first question"),
        ("assistant", "reply 1"),
        ("user", "second question"),
    ]
    assert [m.content for m in agents[0].state.messages] == [
        "first question",
        "reply 1",
        "second question",
        "reply 2",
    ]
    assert agents[0].context_builder.budget == ContextBudget(
        max_tokens=100, response_tokens=20
    )
    assert (tmp_path / ".cairn/models.toml").read_text() == TOML
    assert API_KEY not in result.output
    assert API_KEY not in "".join(
        p.read_text() for p in (tmp_path / ".cairn/traces").glob("*.jsonl")
    )


def test_switch_recounts_with_new_model_before_admitting_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, requests, _ = _configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/model use plus",
        "too big",
        "/model use flash",
        "last",
    )
    counted: list[str] = []

    def count(**kwargs: Any) -> int:
        counted.append(kwargs["model"])
        return 90 if kwargs["model"] == "openai/qwen-plus" else 30

    monkeypatch.setattr(counter_module, "token_counter", count)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0, result.output
    assert "context" in result.output.lower()
    assert "openai/qwen-plus" in counted
    assert counted[0] == counted[-1] == "openai/qwen-flash"
    # 90 input tokens + the unchanged 20-token reserve exceed the 100-token budget.
    assert [request["model"] for request in requests] == ["openai/qwen-flash"] * 2
    assert [m.content for m in agents[0].state.messages] == [
        "first",
        "reply 1",
        "last",
        "reply 2",
    ]


def test_model_commands_validate_input_without_calling_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, requests, token_models = _configure_cli(
        tmp_path,
        monkeypatch,
        "/model use absent",
        "/model use",
        "/model list extra",
        "/model use plus extra",
        "/help model",
        "/model",
    )
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert "Model 'absent' not found" in result.output
    assert "Usage: /model | /model list | /model use <name>" in result.output
    assert "Current model: flash" in result.output
    assert requests == []
    assert token_models == []
    assert agents[0].state.messages == []
    assert not (tmp_path / ".cairn/traces").exists()


def test_failed_switch_keeps_previous_client_counter_and_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, requests, token_models = _configure_cli(
        tmp_path, monkeypatch, "/model use plus", "/model", "question"
    )
    original_counter = counter_module.LiteLLMTokenCounter

    def make_counter(model: str) -> counter_module.LiteLLMTokenCounter:
        if model == "openai/qwen-plus":
            raise ValueError("Cannot initialize selected tokenizer")
        return original_counter(model)

    monkeypatch.setattr(counter_module, "LiteLLMTokenCounter", make_counter)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert "Cannot initialize selected tokenizer" in result.output
    assert "Current model: flash" in result.output
    assert requests[0]["model"] == "openai/qwen-flash"
    assert token_models == ["openai/qwen-flash"]


def test_missing_toml_keeps_legacy_environment_request_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, requests, token_models = _configure_cli(
        tmp_path, monkeypatch, "question", toml=None
    )
    approval = Mock(
        side_effect=AssertionError("Legacy configuration requires no TOML approval")
    )
    monkeypatch.setattr(cli_module, "confirm_model_provider", approval)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert requests[0]["model"] == "openai/legacy-model"
    assert requests[0]["api_key"] == "test-only-legacy-key"
    assert requests[0]["api_base"] == "https://legacy.test/v1"
    assert token_models == ["openai/legacy-model"]
    approval.assert_not_called()


@pytest.mark.parametrize("approval", [False, None, "yes"])
def test_toml_requires_explicit_approval_before_resolving_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approval: object
) -> None:
    agents, requests, _ = _configure_cli(tmp_path, monkeypatch, "question")
    monkeypatch.setattr(cli_module, "confirm_model_provider", lambda _: approval)
    resolve_key = Mock(side_effect=AssertionError("Credential must not be resolved"))
    monkeypatch.setattr(cli_module, "resolve_provider_api_key", resolve_key)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code != 0
    assert "not approved" in str(result.exception)
    assert agents == []
    assert requests == []
    resolve_key.assert_not_called()


@pytest.mark.parametrize(
    "contents", ["", "base_url = [", TOML.replace("https://", "http://")]
)
def test_invalid_existing_toml_never_falls_back_to_valid_legacy_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    agents, requests, _ = _configure_cli(
        tmp_path, monkeypatch, "question", toml=contents
    )
    approval = Mock()
    monkeypatch.setattr(cli_module, "confirm_model_provider", approval)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert str(result.exception)
    assert agents == []
    assert requests == []
    approval.assert_not_called()


def test_dangling_toml_symlink_does_not_use_legacy_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, requests, _ = _configure_cli(tmp_path, monkeypatch, toml=None)
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn/models.toml").symlink_to(tmp_path / "missing.toml")
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, FileNotFoundError)
    assert agents == []
    assert requests == []


def test_approval_uses_loaded_snapshot_even_if_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, requests, _ = _configure_cli(
        tmp_path, monkeypatch, "/model use plus", "question"
    )

    def approve(config: ProviderConfig) -> bool:
        assert config.base_url == "https://example.test/v1"
        (tmp_path / ".cairn/models.toml").write_text(
            TOML.replace("example.test", "unapproved.test").replace(
                "qwen-plus", "other"
            )
        )
        return True

    monkeypatch.setattr(cli_module, "confirm_model_provider", approve)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert requests[0]["api_base"] == "https://example.test/v1"
    assert requests[0]["model"] == "openai/qwen-plus"


@pytest.mark.parametrize("host_key", [None, "host-credential", ""])
def test_toml_credential_uses_host_precedence_and_dotenv_without_environment_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_key: str | None
) -> None:
    agents, requests, _ = _configure_cli(tmp_path, monkeypatch, "question")
    monkeypatch.delenv("BAILIAN_API_KEY")
    if host_key is not None:
        monkeypatch.setenv("BAILIAN_API_KEY", host_key)
    monkeypatch.setattr(
        cli_module, "dotenv_values", lambda: {"BAILIAN_API_KEY": "file-credential"}
    )
    result = CliRunner().invoke(cli_module.app, [])

    if host_key == "":
        assert result.exit_code != 0
        assert "BAILIAN_API_KEY environment variable is not set" in str(
            result.exception
        )
        assert agents == []
        assert requests == []
    else:
        assert result.exit_code == 0
        assert requests[0]["api_key"] == (host_key or "file-credential")
    assert os.environ.get("BAILIAN_API_KEY") == host_key


def test_missing_toml_credential_is_clear_and_does_not_use_legacy_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents, requests, _ = _configure_cli(tmp_path, monkeypatch, "question")
    monkeypatch.delenv("BAILIAN_API_KEY")
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code != 0
    assert "BAILIAN_API_KEY environment variable is not set" in str(result.exception)
    assert agents == []
    assert requests == []


def test_configured_credentials_are_absent_from_real_tool_child_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    require_working_sandbox(Workspace(tmp_path))
    _, requests, _ = _configure_cli(tmp_path, monkeypatch, "inspect environment")
    monkeypatch.setenv("CAIRN_TEST_PASSTHROUGH", "preserved")

    async def completion(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        if len(requests) == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="env-check",
                                    function=SimpleNamespace(
                                        name="bash",
                                        arguments=json.dumps(
                                            {
                                                "command": 'printf "%s|%s|%s" "${BAILIAN_API_KEY-unset}" "${CAIRN_LLM_API_KEY-unset}" "$CAIRN_TEST_PASSTHROUGH"'
                                            }
                                        ),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )
        return _response("checked")

    monkeypatch.setattr(llm_module, "acompletion", completion)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0, result.output
    tool_message = next(m for m in requests[1]["messages"] if m["role"] == "tool")
    tool_result = json.loads(tool_message["content"])
    assert tool_result["exit_code"] == 0
    assert tool_result["stdout"] == "unset|unset|preserved"
    assert os.environ["BAILIAN_API_KEY"] == API_KEY
    for secret in (API_KEY, "test-only-legacy-key"):
        assert secret not in repr(requests[1]["messages"])
        assert secret not in result.output + caplog.text
        assert secret not in "".join(
            p.read_text() for p in (tmp_path / ".cairn/traces").glob("*.jsonl")
        )


def test_provider_error_echoing_key_is_redacted_before_trace_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, requests, _ = _configure_cli(tmp_path, monkeypatch, "question")

    async def completion(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        raise RuntimeError(f"Provider rejected Authorization: Bearer {API_KEY}")

    monkeypatch.setattr(llm_module, "acompletion", completion)
    result = CliRunner().invoke(cli_module.app, [])

    assert result.exit_code == 0
    assert len(requests) == 1  # No automatic fallback to the second configured model.
    assert "credential-bearing details were omitted" in result.output
    traces = "".join(
        p.read_text() for p in (tmp_path / ".cairn/traces").glob("*.jsonl")
    )
    assert "credential-bearing details were omitted" in traces
    assert API_KEY not in traces + result.output + caplog.text
