"""Round evidence and safe recovery metadata for bounded local review."""

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from cairn.git.worktree import WorktreeHandle
from cairn.review.models import ReviewResult, ReviewSeverity, ReviewStatus
from cairn.tasks.models import TaskResult
from cairn.workflow.models import (
    GitSnapshot,
    TaskResultSummary,
    VerificationCheck,
    VerificationResult,
)


class ReviewWorkflowStatus(StrEnum):
    REVIEW_PASSED = "review_passed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REVIEW_ERROR = "review_error"
    REVIEW_INCOMPLETE = "review_incomplete"
    FIX_ERROR = "fix_error"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"


class ReviewWorkflowFailure(StrEnum):
    INVALID_VERIFICATION = "invalid_verification"
    REVIEW_FAILED = "review_failed"
    REVIEW_INCOMPLETE = "review_incomplete"
    ITERATION_LIMIT = "iteration_limit"
    MISSING_FIXER = "missing_fixer"
    MISSING_VERIFIER = "missing_verifier"
    FIX_RUNTIME_ERROR = "fix_runtime_error"
    FIX_BUDGET_EXHAUSTED = "fix_budget_exhausted"
    FIX_CANCELLED = "fix_cancelled"
    FIX_TIMEOUT = "fix_timeout"
    NO_PROGRESS = "no_progress"
    NO_CHANGES = "no_changes"
    SNAPSHOT_DRIFT = "snapshot_drift"
    VERIFICATION_ERROR = "verification_error"
    VERIFICATION_TIMEOUT = "verification_timeout"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"
    GIT_ERROR = "git_error"


@dataclass(frozen=True, slots=True)
class ReviewRound:
    snapshot: GitSnapshot
    verification: VerificationResult
    review: ReviewResult


@dataclass(frozen=True, slots=True)
class ReviewWorkflowResult:
    handle: WorktreeHandle
    snapshot: GitSnapshot | None
    verification: VerificationResult | None
    status: ReviewWorkflowStatus
    failure: ReviewWorkflowFailure | None
    rounds: tuple[ReviewRound, ...]
    fix_results: tuple[TaskResult, ...]
    recovery_path: Path


class FindingLocation(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )
    # Recovery identifiers are harness ordinals, never unrestricted model IDs.
    finding_id: str
    severity: ReviewSeverity
    path: str
    line: int


class ReviewRoundSummary(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )
    tree_revision: str
    review_status: ReviewStatus
    reviewer_trace_id: str | None
    checks: tuple[VerificationCheck, ...]
    findings: tuple[FindingLocation, ...]


class FixSummary(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )
    task: TaskResultSummary
    trace_id: str | None


class ReviewWorkflowReport(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )
    schema_version: int = 1
    status: ReviewWorkflowStatus
    phase: str
    failure: ReviewWorkflowFailure | None
    worktree_path: Path
    branch: str | None
    base_revision: str
    tree_revision: str | None
    verification_checks: tuple[VerificationCheck, ...]
    rounds: tuple[ReviewRoundSummary, ...]
    fixes: tuple[FixSummary, ...]


class ReviewPersistenceError(RuntimeError):
    """Safe round evidence could not be persisted; the worktree is retained."""


def persist_review_report(report: ReviewWorkflowReport, path: Path) -> None:
    if path.resolve().is_relative_to(report.worktree_path.resolve()):
        raise ValueError("Review recovery report must be outside the working tree")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".cairn-review-",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(report.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
