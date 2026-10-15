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
    utc_text,
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


class TaskResolution(StrictDTO):
    """Explicit trusted-host decision about exact inspected Task/Session facts.

    Model text and stored records never supply this approval. No action resumes
    a Turn; inspection authorizes only new, read-only repository observation.
    """

    task_revision: TaskRevision
    session_revision: Revision
    input_digest: Digest
    action: Literal[
        "abandon_without_replay", "acknowledge_checkpoint", "inspect_repository"
    ]
    actor: Reference
    resolved_at_utc: str

    _time = field_validator("resolved_at_utc")(utc_text)

    @model_validator(mode="after")
    def session_identity(self) -> Self:
        if self.task_revision.session_id != self.session_revision.session_id:
            raise ValueError("Invalid Task resolution identity")
        return self


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
    resolution: TaskResolution | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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
        if self.resolution is not None:
            decision = self.resolution
            if (
                decision.task_revision.session_id != self.revision.session_id
                or decision.task_revision.task_id != self.revision.task_id
                or decision.task_revision.version >= self.revision.version
                or decision.input_digest != self.input_digest
                or decision.session_revision.version < self.base_revision.version
            ):
                raise ValueError("Invalid Task resolution")
            if decision.action == "abandon_without_replay":
                if (
                    self.phase != "running"
                    or not self.recovery_required
                    or decision.session_revision.head_seq != self.base_revision.head_seq
                ):
                    raise ValueError("Invalid Task abandonment")
            elif self.checkpoint is None or (
                decision.session_revision.head_seq != self.checkpoint.revision.head_seq
            ):
                raise ValueError("Invalid Task recovery checkpoint")
        return self


class TaskRecoveryReport(StrictDTO):
    task: TaskRecord
    session_revision: Revision
    session_checkpoint: CheckpointReceipt
    status: Literal["incomplete", "recovery_required", "result_recorded", "abandoned"]
    external_effects_may_have_occurred: bool
