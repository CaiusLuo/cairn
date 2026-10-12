"""Strict review evidence; execution status and trace identity belong to the harness."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_REVISION = r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z"
_FINDING_ID = r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\z"
MAX_REVIEW_FINDINGS = 50


class ReviewSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


class ReviewStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    RUNTIME_ERROR = "runtime_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMED_OUT = "timed_out"
    MALFORMED_OUTPUT = "malformed_output"
    INVALID_VERIFICATION = "invalid_verification"
    SNAPSHOT_DRIFT = "snapshot_drift"
    CANCELLED = "cancelled"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    id: str = Field(pattern=_FINDING_ID)
    severity: ReviewSeverity
    summary: str = Field(min_length=1, max_length=500)
    path: str = Field(min_length=1, max_length=4096)
    line: int = Field(ge=1)
    evidence: str = Field(min_length=1, max_length=1000)
    suggested_direction: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("summary", "evidence", "suggested_direction")
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Review text must not be blank")
        return value

    @field_validator("path")
    @classmethod
    def relative_posix_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\0" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Finding path must be a safe relative POSIX path")
        return value


def _validate_unique_findings(findings: tuple[ReviewFinding, ...]) -> None:
    ids = [finding.id for finding in findings]
    locations = [
        (finding.path, finding.line, finding.severity, finding.summary)
        for finding in findings
    ]
    if len(ids) != len(set(ids)) or len(locations) != len(set(locations)):
        raise ValueError("Review findings must have unique identifiers and evidence")


class ReviewResult(BaseModel):
    """Validated findings without unrestricted model output or diagnostic text."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    status: ReviewStatus
    reviewed_tree: str = Field(pattern=_REVISION)
    trace_id: str | None = Field(default=None, pattern=r"\A[0-9a-f]{32}\z")
    findings: tuple[ReviewFinding, ...] = Field(
        default=(), max_length=MAX_REVIEW_FINDINGS
    )

    @model_validator(mode="after")
    def unique_findings(self) -> "ReviewResult":
        _validate_unique_findings(self.findings)
        return self


class _ReviewResponse(BaseModel):
    """The complete model response contract, before harness-owned validation."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    reviewed_tree: str = Field(pattern=_REVISION)
    complete: bool
    findings: tuple[ReviewFinding, ...] = Field(max_length=MAX_REVIEW_FINDINGS)

    @model_validator(mode="after")
    def unique_findings(self) -> "_ReviewResponse":
        _validate_unique_findings(self.findings)
        return self
