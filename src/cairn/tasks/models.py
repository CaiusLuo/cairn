from enum import StrEnum

from pydantic import BaseModel, Field

from cairn.repository import RepositoryEvidence


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"
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
