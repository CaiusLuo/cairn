"""Bounded, immutable data. Approval never comes from deserializing these models."""

import hashlib
import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"\A[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,79}\z")]
Digest = Annotated[str, Field(pattern=r"\A[0-9a-f]{64}\z")]
Oid = Annotated[str, Field(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
MAX_PATHS = 16
MAX_ENTRIES = 256
MAX_ATTESTATIONS = 16
MAX_RECORD_BYTES = 64 * 1024


class MemoryError(RuntimeError):
    """Only fixed, non-sensitive diagnostics cross the memory API boundary."""


class VersionConflict(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory version conflict")


class Kind(StrEnum):
    TESTING = "testing_convention"
    ARCHITECTURE = "architecture_constraint"
    CONTRACT = "cross_module_contract"
    WORKFLOW = "workflow"


class ScopeKind(StrEnum):
    EXACT_PATHS = "exact_paths"
    DIRECTORY_MEMBERSHIP = "directory_membership"
    MANUAL_RECHECK = "manual_recheck"


class Lifecycle(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class Freshness(StrEnum):
    FRESH = "fresh"
    POTENTIALLY_STALE = "potentially_stale"
    STALE = "stale"
    UNKNOWN = "unknown"


class PathKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    MISSING = "missing"
    SYMLINK = "symlink"


class Origin(StrEnum):
    CODE = "code_inspection"
    VERIFICATION = "verification"
    REVIEW = "review"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )


def safe_path(value: str) -> str:
    if (
        not value
        or len(value.encode()) > 512
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(p in {"", ".", "..", ".git"} for p in value.split("/"))
    ):
        raise ValueError("Invalid memory dependency path")
    return value


class ClaimScope(StrictModel):
    kind: ScopeKind
    paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_PATHS)
    # A human must approve completeness; file hashes cannot establish it.
    complete: bool = False

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            safe_path(path)
        if tuple(sorted(set(paths))) != paths:
            raise ValueError("Dependency paths must be unique and sorted")
        return paths


class Candidate(StrictModel):
    statement: str = Field(min_length=1, max_length=1200, repr=False)
    kind: Kind
    scope: ClaimScope
    source_task_id: Identifier | None = None
    trace_id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")] | None = None

    @field_validator("statement")
    @classmethod
    def statement_text(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Statement must be a single nonblank paragraph")
        return value

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Fingerprint(StrictModel):
    path: str
    kind: PathKind
    mode: int = Field(ge=0, le=0o7777)
    sha256: Digest | None = None
    members: int = Field(default=0, ge=0, le=256)

    _path = field_validator("path")(safe_path)

    @model_validator(mode="after")
    def consistent(self) -> "Fingerprint":
        if self.kind in (PathKind.FILE, PathKind.DIRECTORY) and self.sha256 is None:
            raise ValueError("Content or membership fingerprint required")
        if (
            self.kind in (PathKind.MISSING, PathKind.SYMLINK)
            and self.sha256 is not None
        ):
            raise ValueError("Non-followed paths cannot have content")
        if self.kind is PathKind.MISSING and self.mode != 0:
            raise ValueError("Missing path has no mode")
        if self.kind is not PathKind.DIRECTORY and self.members:
            raise ValueError("Only directories have membership counts")
        return self


class EvidenceBinding(StrictModel):
    candidate_digest: Digest
    scope: ClaimScope
    origin: Origin
    # Identifiers describe evidence selected for this claim, not arbitrary checks.
    check_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique_checks(self) -> "EvidenceBinding":
        if len(set(self.check_ids)) != len(self.check_ids):
            raise ValueError("Duplicate check identifiers")
        if self.origin is Origin.VERIFICATION and not self.check_ids:
            raise ValueError("Verification binding requires selected checks")
        if self.origin is Origin.CODE and self.check_ids:
            raise ValueError("Code inspection cannot attest check results")
        return self


class EvidenceCapsule(StrictModel):
    binding: EvidenceBinding
    source_head_oid: Oid | None
    evidence_oid: Oid
    oid_kind: Annotated[str, Field(pattern=r"\Atree\z")] = "tree"
    # The only check verdict representable here is success. Resolution verifies it.
    passed_check_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    review_trace_id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")] | None = None
    dependencies: tuple[Fingerprint, ...] = Field(min_length=1, max_length=MAX_PATHS)

    @model_validator(mode="after")
    def scope_matches(self) -> "EvidenceCapsule":
        if tuple(d.path for d in self.dependencies) != self.binding.scope.paths:
            raise ValueError("Evidence must cover the entire declared scope")
        if self.passed_check_ids != self.binding.check_ids:
            raise ValueError("Evidence checks do not match binding")
        return self


class Attestation(StrictModel):
    capsule: EvidenceCapsule
    # Current checkout HEAD when the dependencies were inspected, distinct from B.
    checkout_head_oid: Oid
    actor: Identifier
    approved_at: int = Field(ge=0)


class MemoryEntry(StrictModel):
    id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")]
    project_id: Digest
    version: int = Field(ge=1)
    created_at: int = Field(ge=0)
    lifecycle: Lifecycle
    candidate: Candidate
    attestations: tuple[Attestation, ...] = Field(
        default=(), max_length=MAX_ATTESTATIONS
    )
    supersedes_id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")] | None = None

    @model_validator(mode="after")
    def provenance_matches(self) -> "MemoryEntry":
        for item in self.attestations:
            if (
                item.capsule.binding.candidate_digest != self.candidate.digest()
                or item.capsule.binding.scope != self.candidate.scope
            ):
                raise ValueError("Attestation belongs to a different claim")
        if (
            self.lifecycle in (Lifecycle.ACCEPTED, Lifecycle.SUPERSEDED)
            and not self.attestations
        ):
            raise ValueError("Accepted knowledge requires an attestation")
        if self.lifecycle is Lifecycle.CANDIDATE and self.attestations:
            raise ValueError("Candidate cannot carry approval")
        return self


_SENSITIVE = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|"
    r"github_pat_[\w]{12,}|AKIA[0-9A-Z]{16})|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[=:]\s*\S+|"
    r"https?://[^\s/]+:[^\s/]+@|[?&](?:token|key|signature|sig)=[^\s&]+)"
)


def check_sensitive(serialized: str, sensitive_values: tuple[str, ...] = ()) -> None:
    if _SENSITIVE.search(serialized) or any(
        v and v in serialized for v in sensitive_values
    ):
        raise MemoryError("Memory contains sensitive content")
