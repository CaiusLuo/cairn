"""Shared isolation helpers for tests that drive the real CLI.

Every CLI test must isolate the same host state: the working directory, dotenv
lookup, the banner, scripted input and LLM/token-count call sites. Those
monkeypatches were copied per module; they live here once so a change to the
CLI's seams lands in one place.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import cairn.infra.llm.token_counter as counter_module
import cairn.terminal.cli as cli_module


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


def set_environment(monkeypatch: Any, environment: dict[str, str]) -> None:
    """Set an exact environment after clearing the shared CLI test variables."""
    for name in environment:
        monkeypatch.setenv(name, environment[name])


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
