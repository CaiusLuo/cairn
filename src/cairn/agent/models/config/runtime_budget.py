"""Read host-only task-budget policy; invalid configuration never falls back."""

import tomllib
from pathlib import Path

from cairn.agent.budget import DEFAULT_MAX_STEPS, RunBudget


def load_task_budget(path: Path | None, *, max_steps: int | None = None) -> RunBudget:
    values = {}
    if path is not None:
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError("Budget configuration is too large")
        data = tomllib.loads(raw.decode())
        if set(data) != {"budget"} or not isinstance(data["budget"], dict):
            raise ValueError("Expected one [budget] table")
        values = data["budget"]
    values.setdefault("max_steps", DEFAULT_MAX_STEPS)
    values.setdefault("max_seconds", 900.0)
    values.setdefault("max_stagnant_steps", 12)
    if max_steps is not None:
        values["max_steps"] = max_steps
    return RunBudget(**values)
