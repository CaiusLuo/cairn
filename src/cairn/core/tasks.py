from enum import StrEnum

from pydantic import BaseModel, Field

from cairn.core.repository import RepositoryEvidence


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"


class TaskCheckStatus(StrEnum):
    """Outcome of an operator-selected host verification command.

    ``passed`` is the only value that can set a task verified. A check that never
    started (a task that did not complete, or a cancellation already set) is
    ``not_run``; a check that started but was interrupted is ``cancelled``; a
    denied shell authorization is ``permission_denied``.
    """

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_RUN = "not_run"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"


class TaskSpec(BaseModel):
    task_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    name: str | None = None


class TaskResult(BaseModel):
    """Completed means normal runtime termination, not verified correct code."""

    task_id: str
    status: TaskStatus
    repository: RepositoryEvidence
    final_response: str | None = None
    trace_id: str | None = None
    error: str | None = None
