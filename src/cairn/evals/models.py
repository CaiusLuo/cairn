from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from cairn.workspace.workspace import Workspace


class EvalStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str | None = None


class EvalResult(BaseModel):
    case_name: str
    status: EvalStatus
    checks: list[CheckResult] = Field(default_factory=list)
    trace_id: str | None = None
    error: str | None = None


class EvalCase(BaseModel):
    name: str
    prompt: str


class EvalCheck(Protocol):
    name: str

    async def evaluate(self, workspace: Workspace) -> CheckResult: ...
