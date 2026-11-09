from pathlib import Path

import pytest

from cairn.agent.budget import DEFAULT_MAX_STEPS, RunBudget
from cairn.agent.models.config.runtime_budget import load_task_budget
from cairn.shared.redaction import StreamRedactor, redact_value


def test_host_budget_defaults_and_explicit_override(tmp_path: Path) -> None:
    assert load_task_budget(None).max_steps == DEFAULT_MAX_STEPS
    path = tmp_path / "budget.toml"
    path.write_text("[budget]\nmax_steps=2000\nmax_tokens=10000\nmax_seconds=60\n")
    assert load_task_budget(path).max_steps == 2000
    assert load_task_budget(path, max_steps=99).max_steps == 99
    path.write_text("[budget]\nunknown=true\n")
    with pytest.raises(TypeError):
        load_task_budget(path)


@pytest.mark.parametrize(
    "values",
    [
        {"max_steps": 0},
        {"max_steps": True},
        {"max_steps": 100001},
        {"max_seconds": float("nan")},
        {"max_seconds": 0},
        {"max_tokens": False},
        {"max_cost": 1},
        {"max_stagnant_steps": 0},
    ],
)
def test_invalid_budgets_fail_closed(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RunBudget(**{"max_steps": 500, **values})  # type: ignore[arg-type]


def test_stream_secrets_are_redacted_across_every_split() -> None:
    secret = "runtime-secret-control"
    text = "prefix " + secret + " suffix"
    for split in range(len(text) + 1):
        stream = StreamRedactor((secret,))
        output = (
            stream.feed(text[:split])
            + stream.feed(text[split:])
            + stream.feed("", final=True)
        )
        assert output == "prefix [redacted] suffix"
    assert redact_value({secret: secret}, (secret,)) == {"[redacted]": "[redacted]"}
