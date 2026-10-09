import json
import sys
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from litellm.exceptions import RateLimitError
from rich.console import Console
from typer.testing import CliRunner

import cairn.cli as cli_module
import cairn.llm.litellm_client as llm_module
import cairn.llm.token_counter as counter_module
import cairn.terminal.output as ui_module
from cairn.assembly import build_agent
from cairn.config import resolve_provider_api_key
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.loop import run_turn
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider, ProviderCatalog
from cairn.llm.provider_runtime import build_provider_runtime
from cairn.observability.storage import TraceStore
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.provider import handle_provider
from cairn.tools.registry import ToolRegistry
from cairn.workspace.workspace import Workspace
from tests.support.runtime import RecordingTool
from tests.support.sandbox import require_working_sandbox

CATALOG_TOML = """\
[[providers]]
name = "bailian"
base_url = "https://bailian.test/v1"
api_key_env = "BAILIAN_API_KEY"

[[providers.models]]
name = "flash"
model_ids = ["openai/qwen-flash"]

[[providers.models]]
name = "plus"
model_ids = ["openai/qwen-plus"]

[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/local-model"]
"""

SINGLE_CATALOG_TOML = """\
[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "flash"
model_ids = ["openai/local-flash"]

[[providers.models]]
name = "plus"
model_ids = ["openai/local-plus"]
"""

LEGACY_TOML = """\
base_url = "https://legacy-toml.test/v1"
api_key_env = "LEGACY_TOML_KEY"

[[models]]
name = "flash"
model_ids = ["openai/legacy-toml-model"]
"""

ENVIRONMENT = {
    "CAIRN_LLM_MODEL": "openai/env-model",
    "CAIRN_LLM_API_KEY": "env-credential",
    "CAIRN_BASE_URL": "https://env.test/v1",
}

BAILIAN_CREDENTIAL = "bailian-only-credential"
LOCAL_CREDENTIAL = "local-only-credential"
LEGACY_TOML_CREDENTIAL = "legacy-toml-only-credential"

CREDENTIALS = {
    "BAILIAN_API_KEY": BAILIAN_CREDENTIAL,
    "LOCAL_API_KEY": LOCAL_CREDENTIAL,
    "LEGACY_TOML_KEY": LEGACY_TOML_CREDENTIAL,
    **ENVIRONMENT,
}


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))
        ]
    )


def configure_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *commands: str,
    toml: str | None = CATALOG_TOML,
    approve: Any = True,
    choose: Any = "bailian",
    group: Any = "flash",
    tools: ToolRegistry | None = None,
) -> SimpleNamespace:
    """Drive the real CLI with fake provider calls and scripted input."""
    monkeypatch.chdir(tmp_path)
    if toml is not None:
        (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".cairn/models.toml").write_text(toml, encoding="utf-8")
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    for name in ("CAIRN_CONTEXT_MAX_TOKENS", "CAIRN_RESPONSE_MAX_TOKENS"):
        monkeypatch.setenv(name, {"CAIRN_CONTEXT_MAX_TOKENS": "100"}.get(name, "20"))
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: {})
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)

    # The session handler is created here so tests can pre-seed grants before
    # the CLI commits any switch.
    handler = SessionPermissionHandler(prompt=ui_module.console_permission_prompt)
    state = SimpleNamespace(
        agents=[],
        requests=[],
        token_models=[],
        contexts=[],
        handler=handler,
        approvals=[],
        resolved_keys=[],
        secret_env_keys=None,
    )

    def confirm_access(
        provider: NamedProvider, selected_group: Any, *, switching: bool
    ) -> bool:
        state.approvals.append((provider.name, selected_group.name, switching))
        if callable(approve):
            return bool(approve(provider, selected_group, switching))
        return bool(approve)

    def choose_one(providers: Sequence[NamedProvider]) -> NamedProvider:
        if callable(choose):
            return cast(NamedProvider, choose(providers))
        return next(provider for provider in providers if provider.name == choose)

    def choose_group(provider: NamedProvider) -> ModelConfig:
        if callable(group):
            return cast(ModelConfig, group(provider))
        return next(
            model for model in provider.config.model_config if model.name == group
        )

    def capture_agent(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        if tools is not None:
            agent.tools = tools
        state.agents.append(agent)
        assert kwargs["permission_handler"] is state.handler
        state.secret_env_keys = kwargs["secret_env_keys"]
        return agent

    real_context = CommandContext

    def capture_context(**kwargs: Any) -> CommandContext:
        context = real_context(**kwargs)
        state.contexts.append(context)
        return context

    real_resolve = resolve_provider_api_key

    def resolve_key(config: Any, host_env: Any, env_file_values: Any) -> str:
        state.resolved_keys.append(config.api_key_env)
        return real_resolve(config, host_env, env_file_values)

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        return _response(f"reply {len(state.requests)}")

    def count(**kwargs: Any) -> int:
        state.token_models.append(kwargs["model"])
        return 30

    monkeypatch.setattr(cli_module, "confirm_provider_access", confirm_access)
    monkeypatch.setattr(cli_module, "choose_provider", choose_one)
    monkeypatch.setattr(cli_module, "choose_model_group", choose_group)
    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    monkeypatch.setattr(
        cli_module, "SessionPermissionHandler", lambda **_kwargs: handler
    )
    monkeypatch.setattr(cli_module, "CommandContext", capture_context)
    monkeypatch.setattr(cli_module, "resolve_provider_api_key", resolve_key)
    monkeypatch.setattr(llm_module, "acompletion", completion)
    monkeypatch.setattr(counter_module, "token_counter", count)
    monkeypatch.setattr(
        cli_module,
        "CliInput",
        lambda: SimpleNamespace(read=AsyncMock(side_effect=[*commands, "/exit"])),
    )
    return state


def run_cli() -> Any:
    return CliRunner().invoke(cli_module.app, [])


def observe_agent(state: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record the runtime bindings visible at the start of every turn."""
    observed: list[tuple[Any, ...]] = []

    async def observing_run_turn(
        agent: Agent, user_input: str, *, budget: RunBudget
    ) -> str:
        observed.append(
            (
                user_input,
                agent.llm,
                agent.context_builder,
                agent.model_executor,
                agent.provider_name,
                agent.state,
                agent.tools,
                agent.permission_handler,
                agent.tracer,
            )
        )
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", observing_run_turn)
    return observed


def test_env_only_startup_keeps_the_legacy_environment_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", toml=None)
    selection = Mock(side_effect=AssertionError("The .env layout has one provider"))
    approval = Mock(side_effect=AssertionError("The .env layout needs no approval"))
    monkeypatch.setattr(cli_module, "choose_provider", selection)
    monkeypatch.setattr(cli_module, "confirm_provider_access", approval)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.requests[0]["model"] == "openai/env-model"
    assert state.requests[0]["api_key"] == "env-credential"
    assert state.requests[0]["api_base"] == "https://env.test/v1"
    assert state.secret_env_keys == frozenset({"CAIRN_LLM_API_KEY"})
    assert "Provider:" not in result.output
    assert not (tmp_path / ".cairn/models.toml").exists()


def test_legacy_toml_startup_keeps_the_single_provider_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", toml=LEGACY_TOML)
    selection = Mock(side_effect=AssertionError("One provider needs no selection"))
    monkeypatch.setattr(cli_module, "choose_provider", selection)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("default", "flash", False)]
    assert state.resolved_keys == ["LEGACY_TOML_KEY"]
    assert state.requests[0]["api_base"] == "https://legacy-toml.test/v1"
    assert state.requests[0]["api_key"] == LEGACY_TOML_CREDENTIAL
    assert "Provider: 'default'" in result.output
    assert "Model group: 'flash' (openai/legacy-toml-model)" in result.output


def test_single_provider_catalog_is_selected_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", toml=SINGLE_CATALOG_TOML)
    selection = Mock(side_effect=AssertionError("One provider needs no selection"))
    group_selection = Mock(
        side_effect=AssertionError("Single-provider startup uses the first group")
    )
    monkeypatch.setattr(cli_module, "choose_provider", selection)
    monkeypatch.setattr(cli_module, "choose_model_group", group_selection)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("local", "flash", False)]
    assert state.requests[0]["api_base"] == "http://localhost:8000/v1"
    assert state.requests[0]["api_key"] == LOCAL_CREDENTIAL
    assert state.requests[0]["model"] == "openai/local-flash"


def test_multiple_providers_require_an_explicit_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path, monkeypatch, "question", choose="local", group="default"
    )
    selected: list[tuple[str, ...]] = []

    def choose(providers: Sequence[NamedProvider]) -> NamedProvider:
        selected.append(tuple(provider.name for provider in providers))
        return next(provider for provider in providers if provider.name == "local")

    monkeypatch.setattr(cli_module, "choose_provider", choose)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert selected == [("bailian", "local")]
    assert state.approvals == [("local", "default", False)]
    assert state.resolved_keys == ["LOCAL_API_KEY"]
    assert state.requests[0]["api_base"] == "http://localhost:8000/v1"
    assert state.requests[0]["api_key"] == LOCAL_CREDENTIAL
    assert state.requests[0]["model"] == "openai/local-model"
    assert BAILIAN_CREDENTIAL not in result.output


def test_multi_provider_startup_allows_explicit_group_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", group="plus")

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("bailian", "plus", False)]
    assert state.requests[0]["model"] == "openai/qwen-plus"
    assert state.token_models == ["openai/qwen-plus"]
    assert "Model group: 'plus' (openai/qwen-plus)" in result.output


def test_multi_provider_startup_rejects_noninteractive_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question")
    monkeypatch.setattr(cli_module, "choose_provider", ui_module.choose_provider)

    result = run_cli()

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "interactive terminal" in str(result.exception)
    assert state.agents == []
    assert state.requests == []
    assert state.approvals == []


def test_multi_provider_startup_rejects_a_cancelled_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question")
    monkeypatch.setattr(
        cli_module,
        "choose_provider",
        Mock(side_effect=ValueError("Provider selection was cancelled.")),
    )

    result = run_cli()

    assert result.exit_code != 0
    assert "cancelled" in str(result.exception)
    assert state.agents == []
    assert state.approvals == []


def test_denied_approval_happens_before_any_credential_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", approve=False)
    resolve_key = Mock(side_effect=AssertionError("Credential must not be resolved"))
    monkeypatch.setattr(cli_module, "resolve_provider_api_key", resolve_key)

    result = run_cli()

    assert result.exit_code != 0
    assert "not approved" in str(result.exception)
    assert state.approvals == [("bailian", "flash", False)]
    assert state.agents == []
    assert state.requests == []
    resolve_key.assert_not_called()


def test_missing_selected_credential_is_reported_without_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", choose="local")
    monkeypatch.delenv("LOCAL_API_KEY")

    result = run_cli()

    assert result.exit_code != 0
    assert "LOCAL_API_KEY environment variable is not set" in str(result.exception)
    assert state.agents == []
    assert state.requests == []


@pytest.mark.parametrize(
    "toml",
    [
        "",
        'base_url = "https://legacy.test/v1"\n' + CATALOG_TOML,
        CATALOG_TOML.replace("https://bailian.test/v1", "http://bailian.test/v1"),
        CATALOG_TOML.replace('name = "local"', 'name = "bailian"'),
    ],
)
def test_invalid_catalog_never_falls_back_to_environment_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question", toml=toml)
    approval = Mock(side_effect=AssertionError("Invalid TOML must not be approved"))
    monkeypatch.setattr(cli_module, "confirm_provider_access", approval)

    result = run_cli()

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert state.agents == []
    assert state.requests == []
    approval.assert_not_called()


def test_every_configured_credential_variable_is_withheld_from_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question")
    assert run_cli().exit_code == 0

    assert state.secret_env_keys == frozenset({"BAILIAN_API_KEY", "LOCAL_API_KEY"})


def test_unselected_provider_credential_is_absent_from_tool_child_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_working_sandbox(Workspace(tmp_path))
    state = configure_cli(tmp_path, monkeypatch, "inspect environment")
    monkeypatch.setenv("CAIRN_TEST_PASSTHROUGH", "preserved")
    command = (
        'printf "%s|%s|%s|%s" "${BAILIAN_API_KEY-unset}" "${LOCAL_API_KEY-unset}" '
        '"${CAIRN_LLM_API_KEY-unset}" "$CAIRN_TEST_PASSTHROUGH"'
    )

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        if len(state.requests) == 1:
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
                                        arguments=json.dumps({"command": command}),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )
        return _response("checked")

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    tool_message = next(
        message
        for message in state.requests[1]["messages"]
        if message["role"] == "tool"
    )
    assert (
        json.loads(tool_message["content"])["stdout"] == "unset|unset|unset|preserved"
    )
    traces = "".join(
        path.read_text() for path in (tmp_path / ".cairn/traces").glob("*.jsonl")
    )
    for secret in (BAILIAN_CREDENTIAL, LOCAL_CREDENTIAL, "env-credential"):
        assert secret not in repr(state.requests[1]["messages"])
        assert secret not in result.output
        assert secret not in traces


def test_provider_command_shows_and_lists_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider", "/provider list")

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "Current provider: bailian" in result.output
    assert "Endpoint: https://bailian.test/v1" in result.output
    assert "Credential variable: BAILIAN_API_KEY" in result.output
    assert "Current model: flash (openai/qwen-flash)" in result.output
    assert "* bailian" in result.output
    assert "  local" in result.output
    assert "endpoint: http://localhost:8000/v1" in result.output
    assert "credential: LOCAL_API_KEY" in result.output
    assert "groups: default" in result.output
    assert state.requests == []


def test_switch_rebinds_runtime_bindings_and_provider_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path, monkeypatch, "first", "/provider use local", "second"
    )
    observed = observe_agent(state, monkeypatch)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert [item[0] for item in observed] == ["first", "second"]
    first, second = observed
    # Conversation state, tools, permission handler and tracer survive a switch.
    for index in (5, 6, 7, 8):
        assert first[index] is second[index]
    assert first[4] == "bailian"
    assert second[4] == "local"
    assert first[1] is not second[1]
    assert first[2] is not second[2]
    assert first[3] is not second[3]

    assert second[1].api_base == "http://localhost:8000/v1"
    assert second[1].api_key == LOCAL_CREDENTIAL
    assert second[1].model == "openai/local-model"
    assert second[2].counter.model == "openai/local-model"
    manager = second[3].manager
    assert manager is state.contexts[0].model_manager
    assert manager.current_model().name == "default"
    assert state.contexts[0].model_executor is second[3]
    assert state.contexts[0].active_provider_name == "local"
    assert [request["api_base"] for request in state.requests] == [
        "https://bailian.test/v1",
        "http://localhost:8000/v1",
    ]
    assert state.token_models == ["openai/qwen-flash", "openai/local-model"]
    assert state.resolved_keys == ["BAILIAN_API_KEY", "LOCAL_API_KEY"]
    assert state.approvals == [("bailian", "flash", False), ("local", "default", True)]


def test_switch_approval_display_names_the_target_before_resolving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_cli(tmp_path, monkeypatch, "/provider use local", "question")
    rendered = StringIO()
    monkeypatch.setattr(
        ui_module,
        "console",
        Console(file=rendered, color_system=None, force_terminal=False, width=200),
    )
    real_confirm = ui_module.confirm_provider_access
    terminal = StringIO("y\ny\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)

    def confirm(provider: Any, group: Any, *, switching: bool) -> bool:
        # CliRunner replaces stdin; give the real prompt a scripted terminal.
        monkeypatch.setattr(sys, "stdin", terminal)
        return real_confirm(provider, group, switching=switching)

    monkeypatch.setattr(cli_module, "confirm_provider_access", confirm)

    result = run_cli()

    assert result.exit_code == 0, result.output
    output = rendered.getvalue()
    assert "Provider: 'local'" in output
    assert "Endpoint: 'http://localhost:8000/v1'" in output
    assert "Credential variable: 'LOCAL_API_KEY'" in output
    assert "Model group: 'default'" in output
    assert "1. 'openai/local-model'" in output
    assert "conversation history" in output
    assert "resets session tool-permission grants" in output
    assert "Current provider: local" in result.output


def test_switch_to_the_active_provider_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use bailian", "question")
    observed = observe_agent(state, monkeypatch)
    handler = state.handler
    handler.grants.add(PermissionCapability.NETWORK)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("bailian", "flash", False)]
    assert state.resolved_keys == ["BAILIAN_API_KEY"]
    assert handler.grants == {PermissionCapability.NETWORK}
    assert observed[0][1] is observed[0][1]
    assert state.contexts[0].active_provider_name == "bailian"
    assert "Current provider: bailian" in result.output
    assert state.requests[0]["api_base"] == "https://bailian.test/v1"


def test_denied_switch_keeps_the_old_provider_fully_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def approve(provider: NamedProvider, group: Any, switching: bool) -> bool:
        return not switching

    state = configure_cli(
        tmp_path,
        monkeypatch,
        "/provider use local",
        "question",
        approve=approve,
    )
    observed = observe_agent(state, monkeypatch)
    handler = state.handler
    handler.grants.add(PermissionCapability.NETWORK)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "Provider 'local' was not approved for this session." in result.output
    assert state.resolved_keys == ["BAILIAN_API_KEY"]
    assert handler.grants == {PermissionCapability.NETWORK}
    assert state.contexts[0].active_provider_name == "bailian"
    assert state.requests[0]["api_base"] == "https://bailian.test/v1"
    assert observed[0][4] == "bailian"
    assert observed[0][1].api_base == "https://bailian.test/v1"


def test_missing_target_credential_keeps_the_old_provider_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use local", "question")
    observed = observe_agent(state, monkeypatch)
    handler = state.handler
    handler.grants.add(PermissionCapability.NETWORK)
    monkeypatch.delenv("LOCAL_API_KEY")

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "LOCAL_API_KEY environment variable is not set" in result.output
    assert state.resolved_keys == ["BAILIAN_API_KEY", "LOCAL_API_KEY"]
    assert handler.grants == {PermissionCapability.NETWORK}
    assert state.contexts[0].active_provider_name == "bailian"
    assert observed[0][1].api_base == "https://bailian.test/v1"
    assert state.requests[0]["api_key"] == BAILIAN_CREDENTIAL


def test_failed_runtime_construction_rolls_back_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use local", "question")
    observed = observe_agent(state, monkeypatch)
    handler = state.handler
    handler.grants.add(PermissionCapability.NETWORK)
    real_build = build_provider_runtime

    def build(provider: NamedProvider, *args: Any, **kwargs: Any) -> Any:
        if provider.name == "local":
            raise RuntimeError("cannot construct the target runtime")
        return real_build(provider, *args, **kwargs)

    monkeypatch.setattr(cli_module, "build_provider_runtime", build)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "could not be activated (RuntimeError)" in result.output
    assert "current provider is unchanged" in result.output
    assert handler.grants == {PermissionCapability.NETWORK}
    assert state.contexts[0].active_provider_name == "bailian"
    assert observed[0][1].api_base == "https://bailian.test/v1"
    assert observed[0][4] == "bailian"
    assert state.requests[0]["api_key"] == BAILIAN_CREDENTIAL


def test_successful_switch_resets_session_permission_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use local", "question")
    handler = state.handler
    handler.grants.add(PermissionCapability.NETWORK)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("bailian", "flash", False), ("local", "default", True)]
    assert handler.grants == set()
    assert state.contexts[0].active_provider_name == "local"
    assert state.requests[0]["api_base"] == "http://localhost:8000/v1"


def test_history_and_tool_results_survive_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry()
    registry.register_tool(RecordingTool())
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "second",
        tools=registry,
    )
    observed = observe_agent(state, monkeypatch)

    calls = 0

    async def completion(**kwargs: Any) -> SimpleNamespace:
        nonlocal calls
        state.requests.append(kwargs)
        calls += 1
        if calls == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="record",
                                        arguments=json.dumps({"value": 42}),
                                    ),
                                )
                            ],
                        )
                    )
                ]
            )
        return _response(f"reply {calls}")

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    agent = state.agents[0]
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    # The switched provider receives the same history, including the assistant
    # tool call and its matching result, and can admit it.
    switched = state.requests[2]["messages"]
    assert [message["role"] for message in switched if message["role"] != "system"] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assistant = next(message for message in switched if message["role"] == "assistant")
    assert assistant["tool_calls"][0]["id"] == "call-1"
    tool = next(message for message in switched if message["role"] == "tool")
    assert tool["tool_call_id"] == "call-1"
    assert observed[0][5] is observed[1][5]
    assert agent.state.messages is observed[1][5].messages


def test_switch_does_not_reload_disk_edits_into_the_approved_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "/provider use bailian",
        "second",
        group="flash",
    )
    config_path = tmp_path / ".cairn/models.toml"

    def rewrite() -> None:
        config_path.write_text(
            CATALOG_TOML.replace("openai/qwen-flash", "openai/disk-only-model"),
            encoding="utf-8",
        )

    approvals: list[tuple[str, tuple[str, ...]]] = []

    def approve(provider: NamedProvider, group: Any, switching: bool) -> bool:
        approvals.append((provider.name, provider.config.model_config[0].model_ids))
        if switching:
            rewrite()
        return True

    monkeypatch.setattr(cli_module, "confirm_provider_access", approve)
    observed = observe_agent(state, monkeypatch)

    result = run_cli()

    assert result.exit_code == 0, result.output
    # Both switches used the approved startup snapshot, never the disk edit.
    assert [item[0] for item in approvals] == ["bailian", "local", "bailian"]
    assert state.requests[0]["model"] == "openai/qwen-flash"
    assert state.requests[1]["model"] == "openai/qwen-flash"
    assert "openai/disk-only-model" not in repr(state.requests)
    assert observed[0][4] == "bailian"


def test_switching_back_starts_the_provider_at_its_first_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "second",
        "/provider use bailian",
        "third",
        group="plus",
    )

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert [request["model"] for request in state.requests] == [
        "openai/qwen-plus",
        "openai/local-model",
        "openai/qwen-flash",
    ]


def test_no_cross_provider_fallback_after_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use local", "question")

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        raise RateLimitError(
            message="throttled", model=kwargs["model"], llm_provider="openai"
        )

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "All candidate models are unavailable" in result.output
    assert {request["api_base"] for request in state.requests} == {
        "http://localhost:8000/v1"
    }
    assert [request["model"] for request in state.requests] == ["openai/local-model"]
    assert "qwen-flash" not in repr(state.requests)


def test_failure_diagnostics_are_scoped_to_the_active_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/model list",
        "/provider use local",
        "/model list",
        "second",
    )

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        if kwargs["api_base"] == "https://bailian.test/v1":
            raise RateLimitError(
                message="free tier exhausted",
                model=kwargs["model"],
                llm_provider="openai",
            )
        return _response("answer")

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    listings = [
        line
        for line in result.output.splitlines()
        if line.startswith(("*", " ")) and "openai/" in line
    ]
    assert any("last failure" in line and "qwen-flash" in line for line in listings)
    assert not any(
        "last failure" in line and "local-model" in line for line in listings
    )
    executor = state.contexts[0].model_executor
    assert executor is state.agents[0].model_executor
    assert executor.failure_for("openai/qwen-flash") is None
    assert executor.failure_for("openai/local-model") is None


def test_context_admission_uses_the_target_counter_after_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "too big",
        "/provider use bailian",
        "second",
    )
    counted: list[str] = []

    def count(**kwargs: Any) -> int:
        counted.append(kwargs["model"])
        return 200 if kwargs["model"] == "openai/local-model" else 30

    monkeypatch.setattr(counter_module, "token_counter", count)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "ContextBudgetExceeded" in result.output
    # The rejected turn was admitted with the target provider's counter, and the
    # session continued on the previous provider afterwards.
    assert counted[0] == "openai/qwen-flash"
    assert "openai/local-model" in counted
    assert counted[-1] == "openai/qwen-flash"
    assert [request["model"] for request in state.requests] == [
        "openai/qwen-flash",
        "openai/qwen-flash",
    ]
    # The rejected turn never entered the preserved history.
    assert [message.content for message in state.agents[0].state.messages] == [
        "first",
        "reply 1",
        "second",
        "reply 2",
    ]


def test_invalid_switch_target_is_rejected_without_touching_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "/provider use absent", "question")
    observed = observe_agent(state, monkeypatch)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "Provider 'absent' is not configured in this session." in result.output
    assert state.approvals == [("bailian", "flash", False)]
    assert state.contexts[0].active_provider_name == "bailian"
    assert observed[0][1].api_base == "https://bailian.test/v1"


def test_catalog_model_edits_only_touch_the_active_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "/model add flash openai/new",
        "/model add default openai/cross-provider",
        "/provider use local",
        "/model add default openai/local-new",
        "/model list",
    )
    path = tmp_path / ".cairn/models.toml"

    result = run_cli()

    assert result.exit_code == 0, result.output
    text = path.read_text(encoding="utf-8")
    assert "group does not exist" in result.output
    assert "openai/cross-provider" not in text
    assert 'model_ids = ["openai/qwen-flash", "openai/new"]' in text
    assert 'model_ids = ["openai/local-model", "openai/local-new"]' in text
    # Edits persist to disk only; the running session keeps its loaded groups.
    assert "* default\n    1. openai/local-model — primary\n" in result.output
    assert state.requests == []


def test_catalog_model_edits_keep_other_providers_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commented = CATALOG_TOML.replace(
        'name = "flash"\nmodel_ids = ["openai/qwen-flash"]',
        'name = "flash" # fast group\nmodel_ids = ["openai/qwen-flash"] # ordered',
    )
    configure_cli(tmp_path, monkeypatch, "/model move flash openai/qwen-flash 1")
    path = tmp_path / ".cairn/models.toml"
    path.write_text(commented, encoding="utf-8")
    before = path.read_bytes()

    result = run_cli()

    assert result.exit_code == 0, result.output
    # A no-op move must not rewrite the file at all.
    assert path.read_bytes() == before

    configure_cli(tmp_path, monkeypatch, "/model add flash openai/new", toml=commented)
    result = run_cli()

    assert result.exit_code == 0, result.output
    text = path.read_text(encoding="utf-8")
    assert 'model_ids = ["openai/qwen-flash", "openai/new"] # ordered' in text
    assert 'name = "flash" # fast group' in text
    assert 'model_ids = ["openai/local-model"]' in text


SHARED_MODEL_CATALOG = """\
[[providers]]
name = "bailian"
base_url = "https://bailian.test/v1"
api_key_env = "BAILIAN_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/shared-model"]

[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/shared-model"]
"""


def test_trace_records_distinguish_providers_that_share_a_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "second",
        toml=SHARED_MODEL_CATALOG,
        group="default",
    )

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert [request["model"] for request in state.requests] == [
        "openai/shared-model",
        "openai/shared-model",
    ]
    assert [request["api_base"] for request in state.requests] == [
        "https://bailian.test/v1",
        "http://localhost:8000/v1",
    ]
    traces = [
        [json.loads(line) for line in path.read_text().splitlines()]
        for path in (tmp_path / ".cairn/traces").glob("*.jsonl")
    ]
    traces.sort(key=lambda spans: spans[-1]["start_time"])
    attempts = [
        span for spans in traces for span in spans if span["name"] == "llm.generate"
    ]
    roots = [span for spans in traces for span in spans if span["name"] == "agent.turn"]
    assert [span["attributes"]["model"] for span in attempts] == [
        "openai/shared-model",
        "openai/shared-model",
    ]
    # Identical model IDs stay attributable to the provider that served them.
    assert [span["attributes"]["provider"] for span in attempts] == ["bailian", "local"]
    assert [span["attributes"]["provider"] for span in roots] == ["bailian", "local"]


def test_model_output_is_never_executed_as_a_provider_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(tmp_path, monkeypatch, "question")

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        return _response("/provider use local")

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert state.approvals == [("bailian", "flash", False)]
    assert state.contexts[0].active_provider_name == "bailian"
    assert {request["api_base"] for request in state.requests} == {
        "https://bailian.test/v1"
    }
    assert "/provider use local" in result.output


def test_provider_rejecting_the_history_keeps_the_session_and_allows_switching_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "first",
        "/provider use local",
        "second",
        "/provider use bailian",
        "third",
    )

    async def completion(**kwargs: Any) -> SimpleNamespace:
        state.requests.append(kwargs)
        if kwargs["api_base"] == "http://localhost:8000/v1":
            raise ValueError("Invalid messages for this provider")
        return _response(f"reply {len(state.requests)}")

    monkeypatch.setattr(llm_module, "acompletion", completion)

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert "Invalid messages for this provider" in result.output
    # The rejected turn is rolled back, the earlier history and workspace stay,
    # and the user can switch back to the provider that accepted it.
    assert [message.content for message in state.agents[0].state.messages] == [
        "first",
        "reply 1",
        "third",
        "reply 3",
    ]
    assert state.contexts[0].active_provider_name == "bailian"
    assert [request["api_base"] for request in state.requests] == [
        "https://bailian.test/v1",
        "http://localhost:8000/v1",
        "https://bailian.test/v1",
    ]


def test_context_view_trimming_after_a_switch_preserves_full_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path, monkeypatch, "first", "/provider use local", "second"
    )
    counted: list[str] = []

    def count(**kwargs: Any) -> int:
        counted.append(kwargs["model"])
        return 20 * len(kwargs["messages"])

    monkeypatch.setattr(counter_module, "token_counter", count)

    result = run_cli()

    assert result.exit_code == 0, result.output
    # The switched provider's counter admitted a trimmed request view...
    assert "openai/local-model" in counted
    assert any(
        isinstance(message["content"], str)
        and message["content"].startswith("Context notice:")
        for message in state.requests[1]["messages"]
    )
    assert "context: omitted 1 older turns" in result.output
    # ...without deleting any original session history.
    assert [message.content for message in state.agents[0].state.messages] == [
        "first",
        "reply 1",
        "second",
        "reply 2",
    ]


def test_provider_command_handles_a_session_without_provider_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context = CommandContext(trace_store=TraceStore(tmp_path / "traces"))
    provider = NamedProvider(
        name="local",
        config=ProviderConfig(
            base_url="http://localhost:8000/v1",
            api_key_env="LOCAL_API_KEY",
            model_config=(ModelConfig("default", ("openai/local-model",)),),
        ),
    )

    handle_provider(context, [])
    assert "No provider configuration is available" in capsys.readouterr().out

    context.provider_catalog = ProviderCatalog(providers=(provider,))
    handle_provider(context, [])
    assert "No provider is active" in capsys.readouterr().out

    context.active_provider_name = "local"
    handle_provider(context, [])
    output = capsys.readouterr().out
    assert "Current provider: local" in output
    assert "Current model" not in output

    handle_provider(context, ["use", "local"])
    assert "Usage: /provider" in capsys.readouterr().out


def test_model_use_after_a_switch_targets_the_active_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = configure_cli(
        tmp_path,
        monkeypatch,
        "/provider use bailian",
        "/model use plus",
        "question",
        choose="local",
        group="default",
    )

    result = run_cli()

    assert result.exit_code == 0, result.output
    assert [request["model"] for request in state.requests] == ["openai/qwen-plus"]
    assert state.requests[0]["api_base"] == "https://bailian.test/v1"
    assert state.requests[0]["api_key"] == BAILIAN_CREDENTIAL
    assert state.agents[0].provider_name == "bailian"
    assert state.agents[0].context_builder.counter.model == "openai/qwen-plus"
    manager = state.contexts[0].model_manager
    assert manager is state.agents[0].model_executor.manager
    assert manager.current_model().name == "plus"
