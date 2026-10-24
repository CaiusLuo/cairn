"""Run-budget resolution for the CLI entry point."""

import pytest
import typer

import cairn.terminal.cli as cli_module
from cairn.infra.config import CAIRN_MAX_STEPS_ENV_NAME


@pytest.mark.parametrize(
    "environ",
    [{}, {CAIRN_MAX_STEPS_ENV_NAME: "   "}],
    ids=["unset", "blank"],
)
def test_absent_or_blank_env_uses_the_documented_default(
    environ: dict[str, str],
) -> None:
    budget = cli_module.resolve_cli_run_budget(environ)

    assert budget.max_steps == cli_module.DEFAULT_CLI_MAX_STEPS
    assert budget.max_steps == 150


@pytest.mark.parametrize(("value", "expected"), [("300", 300), ("1", 1)])
def test_env_override_sets_the_budget(value: str, expected: int) -> None:
    budget = cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: value})

    assert budget.max_steps == expected


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5"])
def test_invalid_env_override_is_rejected_not_silently_ignored(value: str) -> None:
    """A bad ceiling must fail loudly; falling back would widen authority."""
    with pytest.raises(typer.BadParameter):
        cli_module.resolve_cli_run_budget({CAIRN_MAX_STEPS_ENV_NAME: value})
