from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, Field, computed_field

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.workspace.workspace import Workspace


class EvalStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str | None = None
    error: str | None = None


class EvalMetrics(BaseModel):
    """Observed case metadata; absent observations stay null.

    Provider usage comes only from trace_finish, never context estimates.
    Trim counts are the largest observed omission, not sums of repeated views.
    """

    model_identifier: str | None = None
    trace_id: str | None = None
    elapsed_seconds: float | None = None
    agent_steps_used: int | None = None
    context_trimmed_turns: int | None = None
    context_trimmed_messages: int | None = None
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None
    token_counter_implementation: str | None = None
    request_token_counts_estimated: bool | None = None
    provider_max_output_tokens: int | None = None


class EvalResult(BaseModel):
    case_name: str
    status: EvalStatus
    checks: list[CheckResult] = Field(default_factory=list)
    trace_id: str | None = None
    error: str | None = None
    metrics: EvalMetrics | None = None


class EvalCase(BaseModel):
    name: str
    prompt: str
    files: dict[str, str] = Field(default_factory=dict)


class EvalCheck(Protocol):
    name: str

    async def evaluate(self, workspace: Workspace) -> CheckResult: ...


@dataclass(frozen=True, slots=True)
class EvalSuiteCase:
    case: EvalCase
    checks: tuple[EvalCheck, ...]

    def __post_init__(self) -> None:
        if not self.case.name.strip():
            raise ValueError("case name must be nonempty")
        if not self.checks:
            raise ValueError("each suite case must have checks")


@dataclass(frozen=True, slots=True)
class EvalSuite:
    name: str
    cases: tuple[EvalSuiteCase, ...]

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.cases:
            raise ValueError("suite name and cases must be nonempty")
        names = [item.case.name for item in self.cases]
        if len(names) != len(set(names)):
            raise ValueError("suite case names must be unique")


class EvalSuiteConfig(BaseModel):
    run_budget: RunBudget
    context_budget: ContextBudget
    run_timeout_seconds: float
    check_timeout_seconds: float


class EvalSuiteCounts(BaseModel):
    total: int
    completed: int
    passed: int
    failed: int
    errors: int


class EvalSuiteResult(BaseModel):
    schema_version: Literal[1] = 1
    suite_name: str
    case_names: tuple[str, ...]
    config: EvalSuiteConfig
    state: Literal["running", "completed", "interrupted"] = "running"
    active_case_name: str | None = None
    active_case_metrics: EvalMetrics | None = None
    results: list[EvalResult] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> EvalSuiteCounts:
        return EvalSuiteCounts(
            total=len(self.case_names),
            completed=len(self.results),
            passed=sum(result.status is EvalStatus.PASS for result in self.results),
            failed=sum(result.status is EvalStatus.FAIL for result in self.results),
            errors=sum(result.status is EvalStatus.ERROR for result in self.results),
        )
