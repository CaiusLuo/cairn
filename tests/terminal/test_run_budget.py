"""Run-budget resolution for the CLI and Pi TUI entry points."""

from pathlib import Path

import pytest
import typer

import cairn.terminal.cli as cli_module
from cairn.infra.config_schema import CAIRN_MAX_STEPS_ENV_NAME


def test_unset_env_uses_the_documented_default() -> None:
    budget = cli_module.resolve_cli_run_budget({})

    assert budget.max_steps == cli_module.DEFAULT_CLI_MAX_STEPS
    assert budget.max_steps == 150


def test_env_override_selects_a_longer_budget() -> None:
    budget = cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: "300"})

    assert budget.max_steps == 300


def test_env_override_accepts_one_step() -> None:
    budget = cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: "1"})

    assert budget.max_steps == 1


def test_blank_env_override_falls_back_to_the_default() -> None:
    budget = cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: "   "})

    assert budget.max_steps == cli_module.DEFAULT_CLI_MAX_STEPS


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5"])
def test_invalid_env_override_is_rejected_not_silently_ignored(value: str) -> None:
    """A bad ceiling must fail loudly; falling back would widen authority."""
    with pytest.raises(typer.BadParameter):
        cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: value})


def test_tui_and_cli_share_one_budget_source() -> None:
    """bridge.py previously hard-coded 50; it must reuse the CLI's budget."""
    cli_source = Path("src/cairn/terminal/cli.py").read_text(encoding="utf-8")
    bridge_source = Path("src/cairn/terminal/bridge.py").read_text(encoding="utf-8")

    assert "from cairn.terminal.cli import DEFAULT_CLI_RUN_BUDGET" in bridge_source
    assert "RunBudget(max_steps=50)" not in bridge_source
    assert "DEFAULT_CLI_MAX_STEPS = 150" in cli_source
