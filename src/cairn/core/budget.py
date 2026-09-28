from dataclasses import dataclass
from enum import StrEnum


class BudgetReason(StrEnum):
    MAX_STEPS = "max_steps"


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_steps: int

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")


class RunBudgetExceeded(Exception):
    def __init__(
        self,
        *,
        reason: BudgetReason,
        limit: int,
        used: int,
    ) -> None:
        self.reason = reason
        self.limit = limit
        self.used = used

        super().__init__(
            f"Run budget exhausted: {reason.value} (used {used}, limit {limit})"
        )
