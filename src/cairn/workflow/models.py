"""Safe, immutable evidence for one locally verified Issue task."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from cairn.git.worktree import WorktreeHandle

_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CHECK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:#/-]{0,99}\Z")


class WorkflowPhase(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    STAGING = "staging"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    AUTHORIZING = "authorizing"
    COMMITTING = "committing"
    PUBLISHING = "publishing"
    CREATING_PR = "creating_pr"
    PUBLISHED = "published"


class WorkflowStatus(StrEnum):
    BLOCKED = "blocked"
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    PUBLISHED = "published"


class WorkflowFailure(StrEnum):
    RUNNER_CONFIGURATION = "runner_configuration"
    TASK_BUDGET_EXHAUSTED = "task_budget_exhausted"
    TASK_RUNTIME_ERROR = "task_runtime_error"
    TASK_CANCELLED = "task_cancelled"
    NO_CHANGES = "no_changes"
    GIT_ERROR = "git_error"
    MISSING_VERIFIER = "missing_verifier"
    MALFORMED_VERIFICATION = "malformed_verification"
    STALE_VERIFICATION = "stale_verification"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_ERROR = "verification_error"
    DRIFT = "drift"
    CANCELLED = "cancelled"
    PERMISSION_DENIED = "permission_denied"
    PUBLISH_FAILED = "publish_failed"


class VerificationCategory(StrEnum):
    CHECK_FAILED = "check_failed"
    CHECK_TIMEOUT = "check_timeout"
    CHECK_ERROR = "check_error"


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    head_revision: str
    tree_revision: str
    branch: str
    changed_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            for revision in (self.head_revision, self.tree_revision)
        ):
            raise ValueError("Invalid staged snapshot revision")
        if not isinstance(self.branch, str) or not self.branch:
            raise ValueError("Staged snapshot requires a branch")
        if (
            type(self.changed_files) is not tuple
            or not self.changed_files
            or any(not isinstance(path, str) or not path for path in self.changed_files)
            or len(self.changed_files) != len(set(self.changed_files))
        ):
            raise ValueError("Staged snapshot requires unique changed file paths")


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    check_id: str
    passed: bool
    category: VerificationCategory | None = None

    def validate(self) -> None:
        if not isinstance(self.check_id, str) or not _CHECK_ID.fullmatch(self.check_id):
            raise ValueError("Invalid verification check identifier")
        if type(self.passed) is not bool:
            raise ValueError("Verification verdict must be a boolean")
        if (
            self.category is not None
            and type(self.category) is not VerificationCategory
        ):
            raise ValueError("Invalid verification category")
        if self.passed and self.category is not None:
            raise ValueError("A passed check cannot have a failure category")

    def __post_init__(self) -> None:
        self.validate()


@dataclass(frozen=True, slots=True)
class VerificationResult:
    tree_revision: str
    checks: tuple[VerificationCheck, ...]

    def validate(self) -> None:
        if not isinstance(self.tree_revision, str) or not _REVISION.fullmatch(
            self.tree_revision
        ):
            raise ValueError("Invalid verified tree revision")
        if type(self.checks) is not tuple or not self.checks:
            raise ValueError("Verification requires a nonempty tuple of checks")
        for check in self.checks:
            if type(check) is not VerificationCheck:
                raise ValueError("Invalid verification check")
            check.validate()
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("Verification check identifiers must be unique")

    def __post_init__(self) -> None:
        self.validate()

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


class WorkflowReport(BaseModel):
    """Only harness-owned metadata; never model text or command output."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    schema_version: int = 1
    source_url: str
    issue_number: int
    repository: str
    base_branch: str
    intended_fix: bool
    branch: str
    worktree_path: Path
    base_revision: str
    phase: WorkflowPhase
    status: WorkflowStatus
    failure: WorkflowFailure | None = None
    head_revision: str | None = None
    tree_revision: str | None = None
    changed_files: tuple[str, ...] = ()
    trace_id: str | None = None
    checks: tuple[VerificationCheck, ...] = ()
    commit: str | None = None
    remote_branch: str | None = None
    remote_published: bool | None = False
    pr_url: str | None = None


@dataclass(frozen=True, slots=True)
class LocalWorkflowResult:
    handle: WorktreeHandle
    snapshot: GitSnapshot | None
    verification: VerificationResult | None
    report: WorkflowReport
    recovery_path: Path


def persist_report(report: WorkflowReport, path: Path) -> None:
    """Atomically write a private report outside the retained working tree."""
    if path.resolve().is_relative_to(report.worktree_path.resolve()):
        raise ValueError("Workflow report must be outside the working tree")
    payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=True, indent=2)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".cairn-report-",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
