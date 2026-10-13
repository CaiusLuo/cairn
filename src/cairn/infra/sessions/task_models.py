"""Bounded Task facts, without prompts, execution objects or recovery authority."""

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from cairn.infra.sessions.models import (
    CheckpointReceipt,
    Counter,
    Digest,
    Outcome,
    Reference,
    Revision,
    StrictDTO,
    uuid4_text,
)

MAX_TASK_BYTES = 4096
TaskPhase = Literal["running", "turn_committed", "inspecting", "result_recorded"]
TaskDiagnostic = Literal["storage_failed", "inspection_failed", "interrupted"]


class TaskRevision(StrictDTO):
    session_id: str
    task_id: Reference
    version: Counter

    _session = field_validator("session_id")(uuid4_text)


class TaskRepositoryObservation(StrictDTO):
    """Historical public facts only; no paths, error text or Git authority."""

    is_git_repository: bool | None = None
    head_revision: (
        Annotated[str, Field(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")] | None
    ) = None
    dirty: bool | None = None

    @model_validator(mode="after")
    def git_facts(self) -> Self:
        if self.is_git_repository is not True and (
            self.head_revision is not None or self.dirty is not None
        ):
            raise ValueError("Invalid repository observation")
        return self


class TaskResultData(StrictDTO):
    """Host-selected result fields, deliberately not a serialized TaskResult."""

    status: Outcome
    trace_id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")] | None = None
    repository: TaskRepositoryObservation | None = None


class TaskRecord(StrictDTO):
    revision: TaskRevision
    input_digest: Digest
    phase: TaskPhase
    base_revision: Revision
    checkpoint: CheckpointReceipt | None = None
    result: TaskResultData | None = None
    recovery_required: bool = False
    diagnostic: TaskDiagnostic | None = None

    @model_validator(mode="after")
    def relationships(self) -> Self:
        if self.base_revision.session_id != self.revision.session_id:
            raise ValueError("Invalid Task session")
        minimum = ("running", "turn_committed", "inspecting", "result_recorded").index(
            self.phase
        )
        if self.revision.version < minimum:
            raise ValueError("Invalid Task revision")
        if (self.phase == "running") != (self.checkpoint is None):
            raise ValueError("Invalid Task checkpoint")
        if self.checkpoint is not None and (
            self.checkpoint.revision.session_id != self.revision.session_id
            or self.checkpoint.revision.head_seq != self.base_revision.head_seq + 1
            or self.checkpoint.revision.version <= self.base_revision.version
        ):
            raise ValueError("Invalid Task checkpoint")
        if (self.phase == "result_recorded") != (self.result is not None):
            raise ValueError("Invalid Task result")
        if self.recovery_required != (self.diagnostic is not None) or (
            self.phase == "result_recorded" and self.recovery_required
        ):
            raise ValueError("Invalid Task diagnostic")
        if (
            self.result is not None
            and self.result.status == "completed"
            and (self.checkpoint is None or self.checkpoint.outcome != "completed")
        ):
            raise ValueError("Invalid completed Task result")
        return self


class TaskRecoveryReport(StrictDTO):
    task: TaskRecord
    session_revision: Revision
    session_checkpoint: CheckpointReceipt
    status: Literal["incomplete", "recovery_required", "result_recorded"]
    external_effects_may_have_occurred: bool
