"""Shared isolation helpers for tests that drive the real CLI.

Every CLI test must isolate the same host state: the working directory, dotenv
lookup, the banner, scripted input and LLM/token-count call sites. Those
monkeypatches were copied per module; they live here once so a change to the
CLI's seams lands in one place.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import cairn.infra.llm.litellm_client as llm_module
import cairn.infra.llm.token_counter as counter_module
import cairn.terminal.cli as cli_module
import cairn.terminal.output as ui_module
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    SessionPermissionHandler,
)
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.config import resolve_provider_api_key
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.assembly import build_agent
from cairn.terminal.commands.context import CommandContext
from tests.support.streaming import CompletionResponse


def completion_response(content: str) -> SimpleNamespace:
    return CompletionResponse(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))
        ]
    )


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


def configure_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *commands: str,
    toml: str | None = CATALOG_TOML,
    approve: Any = True,
    tools: ToolRegistry | None = None,
    environment: dict[str, str] = CREDENTIALS,
) -> SimpleNamespace:
    """Drive the real CLI with fake provider calls and scripted input."""
    if toml is not None:
        write_models_toml(tmp_path, toml)
    for name in environment:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    isolate_cli(tmp_path, monkeypatch, *commands)
    set_limits(monkeypatch)

    # The session handler is created here so tests can pre-seed grants before
    # the CLI commits any switch.
    def prompt(request: Any) -> PermissionChoice:
        # A local shell is approved for the session: these tests exercise the
        # command and its environment, not the approval dialogue itself.
        if request.capability is PermissionCapability.LOCAL_SHELL:
            return PermissionChoice.ALLOW_SESSION
        return ui_module.console_permission_prompt(request)

    handler = SessionPermissionHandler(prompt=prompt)
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
        return completion_response(f"reply {len(state.requests)}")

    monkeypatch.setattr(cli_module, "confirm_provider_access", confirm_access)
    # The started directory is trusted for these runs; the trust dialogue has its
    # own test and a CLI test has no interactive terminal.
    monkeypatch.setattr(cli_module, "confirm_workspace_trust", lambda root: True)
    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    monkeypatch.setattr(
        cli_module, "SessionPermissionHandler", lambda **_kwargs: handler
    )
    monkeypatch.setattr(cli_module, "CommandContext", capture_context)
    monkeypatch.setattr(cli_module, "resolve_provider_api_key", resolve_key)
    monkeypatch.setattr(llm_module, "acompletion", completion)
    state.token_models = count_tokens_as(monkeypatch)
    return state


class ScriptedInput(SimpleNamespace):
    """A `CliInput` replacement that yields scripted values then `/exit`."""

    def __init__(self, *values: str) -> None:
        super().__init__(
            read=AsyncMock(side_effect=[*values, "/exit"]),
        )


def isolate_cli(
    tmp_path: Any,
    monkeypatch: Any,
    *commands: str,
) -> None:
    """Apply the shared CLI isolation: cwd, dotenv, banner and scripted input.

    Callers add their own provider, credential and LLM monkeypatches on top.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: {})
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    monkeypatch.setattr(cli_module, "CliInput", lambda: ScriptedInput(*commands))


def set_limits(monkeypatch: Any, *, context: str = "100", response: str = "20") -> None:
    """Pin the token budgets the CLI resolves from the environment."""
    monkeypatch.setenv("CAIRN_CONTEXT_MAX_TOKENS", context)
    monkeypatch.setenv("CAIRN_RESPONSE_MAX_TOKENS", response)


def write_models_toml(tmp_path: Any, toml: str) -> None:
    """Create `.cairn/models.toml` with the given contents."""
    (tmp_path / ".cairn").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cairn/models.toml").write_text(toml, encoding="utf-8")


def count_tokens_as(monkeypatch: Any, value: int = 30) -> list[str]:
    """Replace the token counter with a fixed value, recording each model."""
    models: list[str] = []

    def count(**kwargs: Any) -> int:
        models.append(kwargs["model"])
        return value

    monkeypatch.setattr(counter_module, "token_counter", count)
    return models
