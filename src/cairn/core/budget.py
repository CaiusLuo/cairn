from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

DEFAULT_MAX_STEPS = 500


class BudgetReason(StrEnum):
    MAX_STEPS = "max_steps"
    MAX_SECONDS = "max_seconds"
    CHECKPOINT_CAPACITY = "checkpoint_capacity"
    MAX_TOKENS = "max_tokens"
    MAX_COST = "max_cost"
    NO_PROGRESS = "no_progress"
    USAGE_UNKNOWN = "usage_unknown"


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_steps: int
    max_seconds: float | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    max_stagnant_steps: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    def __post_init__(self) -> None:
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 100000:
            raise ValueError("max_steps must be at least 1 and at most 100000")
        for name in ("max_tokens", "max_stagnant_steps"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "max_seconds",
            "max_cost",
            "input_cost_per_million",
            "output_cost_per_million",
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float)
                or not isfinite(value)
                or value < 0
                or (name.startswith("max_") and value == 0)
            ):
                raise ValueError(f"{name} must be finite and positive")
        if self.max_cost is not None and (
            self.input_cost_per_million is None or self.output_cost_per_million is None
        ):
            raise ValueError(
                "A cost budget requires host-selected rate ceilings for all approved models"
            )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int
    requests: int
    tool_attempts: int
    elapsed_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    stagnant_steps: int
    stop_reason: str | None


class RunBudgetExceeded(Exception):
    def __init__(
        self, *, reason: BudgetReason, limit: int | float, used: int | float
    ) -> None:
        self.reason = reason
        self.limit = limit
        self.used = used
        super().__init__(
            f"Run budget exhausted: {reason.value} (used {used}, limit {limit})"
        )
