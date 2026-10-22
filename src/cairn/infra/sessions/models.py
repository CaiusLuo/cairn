"""Immutable storage facts, never execution or permission capabilities."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_INTEGER = (1 << 63) - 1
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_INVOCATIONS = 256
MAX_RECORD_BYTES = 64 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_WAL_BYTES = 32 * 1024 * 1024
FORMAT_VERSION = 1
SCHEMA_VERSION = 4

Counter = Annotated[int, Field(ge=0, le=MAX_INTEGER)]
Digest = Annotated[str, Field(pattern=r"\A[0-9a-f]{64}\z")]


def reference_text(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid reference")
    return value


Reference = Annotated[
    str, Field(min_length=1, max_length=256), AfterValidator(reference_text)
]
Outcome = Literal["completed", "cancelled", "budget_exhausted", "runtime_error"]
Reason = Literal["storage_failed", "finalization_failed", "binding_unavailable"]
TurnState = Literal[
    "executing", "completed", "interrupted", "recovery_required", "abandoned"
]
SessionState = Literal["ready", "executing", "recovery_required"]


class SessionStoreError(RuntimeError):
    """Fixed diagnostics only; callers must not attach raw storage exceptions.

    ``code`` stays a fixed literal that callers may compare or match. A
    host-actionable failure may add one fixed-shape ``detail`` string built from
    host facts the operator needs (a local version, a required range, the
    declaration to set) and never from storage text, SQL or a payload.
    """

    def __init__(self, code: str = "storage_unavailable", detail: str = "") -> None:
        # Internal callers select literals, never text from SQLite or a payload.
        self.code = code
        self.detail = detail
        message = f"Session store: {code}"
        super().__init__(f"{message}: {detail}" if detail else message)


class VersionConflict(SessionStoreError):
    def __init__(self) -> None:
        super().__init__("revision_conflict")


class StrictDTO(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


def uuid4_text(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Invalid identity")
    return value


def utc_text(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
        raise ValueError("Invalid observation time")
    datetime.fromisoformat(value)
    return value


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def absolute_path(value: str) -> str:
    if (
        not value.startswith("/")
        or len(value.encode("utf-8")) > 4096
        or any(ord(c) < 32 for c in value)
        or "\\" in value
        or (value != "/" and any(p in ("", ".", "..") for p in value[1:].split("/")))
    ):
        raise ValueError("Invalid observed path")
    return value


class PathIdentity(StrictDTO):
    path: str
    device: Counter
    inode: Counter

    _path = field_validator("path")(absolute_path)


class GitBinding(StrictDTO):
    repository: PathIdentity
    common: PathIdentity
    admin: PathIdentity
    workspace_prefix: str = Field(max_length=4096)
    registration_digest: Digest
    head: Annotated[str, Field(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")] | None
    branch: Reference | None

    @field_validator("workspace_prefix")
    @classmethod
    def prefix(cls, value: str) -> str:
        if value and (
            value.startswith("/")
            or not value.endswith("/")
            or any(p in ("", ".", "..") for p in value[:-1].split("/"))
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Invalid workspace prefix")
        return value


class WorkspaceBinding(StrictDTO):
    root: PathIdentity
    git: GitBinding | None  # None is a host-observed non-Git workspace.


class ProviderReference(StrictDTO):
    provider: Reference
    model_group: Reference
    model_ids: tuple[Reference, ...] = Field(min_length=1, max_length=32)
    config_digest: Digest
    system_prompt_digest: Digest
    context_budget: Counter
    output_reserve: Counter
    max_steps: Annotated[int, Field(ge=1, le=MAX_INTEGER)]


class SessionBinding(StrictDTO):
    session_id: str
    workspace: WorkspaceBinding
    provider: ProviderReference | None
    observed_at_utc: str

    _id = field_validator("session_id")(uuid4_text)
    _time = field_validator("observed_at_utc")(utc_text)


def same_workspace(left: SessionBinding, right: SessionBinding) -> bool:
    """HEAD/branch/provider may change; root and registration identity may not."""
    a, b = left.workspace, right.workspace
    if left.session_id != right.session_id or a.root != b.root:
        return False
    if a.git is None or b.git is None:
        return a.git == b.git
    return a.git.model_copy(update={"head": None, "branch": None}) == b.git.model_copy(
        update={"head": None, "branch": None}
    )


class Revision(StrictDTO):
    session_id: str
    version: Counter
    head_seq: Counter

    _id = field_validator("session_id")(uuid4_text)

    @model_validator(mode="after")
    def sequence(self) -> Self:
        if self.version < self.head_seq:
            raise ValueError("Invalid revision sequence")
        return self


class TurnIntent(StrictDTO):
    session_id: str
    turn_id: str
    base_seq: Counter
    message_start: Annotated[int, Field(ge=0, le=MAX_MESSAGES)]
    input_digest: Digest
    started_at_utc: str
    binding: SessionBinding
    task_id: Reference | None = None

    _ids = field_validator("session_id", "turn_id")(uuid4_text)
    _time = field_validator("started_at_utc")(utc_text)

    @model_validator(mode="after")
    def relationship(self) -> Self:
        if self.binding.session_id != self.session_id or instant(
            self.binding.observed_at_utc
        ) > instant(self.started_at_utc):
            raise ValueError("Invalid intent binding")
        return self


class ToolFence(StrictDTO):
    session_id: str
    turn_id: str
    ordinal: Annotated[int, Field(ge=1, le=MAX_INVOCATIONS)]
    assistant_message_index: Annotated[int, Field(ge=0, lt=MAX_MESSAGES)]
    call_id: Reference
    tool_name: Reference
    observed_at_utc: str

    _ids = field_validator("session_id", "turn_id")(uuid4_text)
    _time = field_validator("observed_at_utc")(utc_text)


class StoredToolCall(StrictDTO):
    id: Reference
    name: Reference
    # Canonical JSON object text makes arguments immutable, including nested values.
    # Codec validates its JSON/type/size/secrets before any write or restoration.
    arguments_json: str = Field(repr=False)


class StoredMessage(StrictDTO):
    role: Literal["user", "assistant", "tool"]
    content: str | None = Field(default=None, repr=False)
    tool_call_id: Reference | None = None
    tool_calls: tuple[StoredToolCall, ...] = Field(
        default=(), max_length=MAX_INVOCATIONS
    )

    @model_validator(mode="after")
    def role_fields(self) -> Self:
        if self.role == "tool":
            if self.tool_call_id is None or self.content is None or self.tool_calls:
                raise ValueError("Invalid tool message")
        elif self.tool_call_id is not None or (
            self.role == "user" and (self.content is None or self.tool_calls)
        ):
            raise ValueError("Invalid message fields")
        return self


class MessageRange(StrictDTO):
    start: Annotated[int, Field(ge=0, le=MAX_MESSAGES)]
    end: Annotated[int, Field(ge=0, le=MAX_MESSAGES)]


class TraceSummary(StrictDTO):
    trace_id: Annotated[str, Field(pattern=r"\A[0-9a-f]{32}\z")] | None = None
    persisted: bool | None = None
    input_tokens: Counter | None = None
    output_tokens: Counter | None = None
    error_code: Literal["sink_failed"] | None = None

    @model_validator(mode="after")
    def nullable(self) -> Self:
        if (
            self.trace_id is None
            and any(
                v is not None
                for v in (
                    self.persisted,
                    self.input_tokens,
                    self.output_tokens,
                    self.error_code,
                )
            )
        ) or (self.error_code is not None and self.persisted is not False):
            raise ValueError("Invalid trace summary")
        return self


class CheckpointPayload(StrictDTO):
    format_version: Literal[1] = 1
    session_id: str
    turn_id: str | None
    seq: Counter
    binding: SessionBinding
    messages: tuple[StoredMessage, ...] = Field(max_length=MAX_MESSAGES, repr=False)
    message_range: MessageRange
    outcome: Outcome | None
    started_at_utc: str | None
    settled_at_utc: str
    trace: TraceSummary = Field(default_factory=TraceSummary)

    _session = field_validator("session_id")(uuid4_text)
    _settle = field_validator("settled_at_utc")(utc_text)

    @field_validator("format_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != FORMAT_VERSION:
            raise ValueError("Invalid format version")
        return value

    @field_validator("turn_id")
    @classmethod
    def turn(cls, value: str | None) -> str | None:
        return None if value is None else uuid4_text(value)

    @field_validator("started_at_utc")
    @classmethod
    def start_time(cls, value: str | None) -> str | None:
        return None if value is None else utc_text(value)

    @model_validator(mode="after")
    def envelope(self) -> Self:
        if self.binding.session_id != self.session_id or not (
            self.message_range.start <= self.message_range.end == len(self.messages)
        ):
            raise ValueError("Invalid checkpoint range or binding")
        if instant(self.binding.observed_at_utc) > instant(self.settled_at_utc):
            raise ValueError("Invalid checkpoint observation time")
        if self.seq == 0:
            if (
                self.turn_id is not None
                or self.outcome is not None
                or self.started_at_utc is not None
                or self.messages
                or self.trace != TraceSummary()
            ):
                raise ValueError("Invalid initial checkpoint")
        elif (
            self.turn_id is None
            or self.outcome is None
            or self.started_at_utc is None
            or instant(self.started_at_utc) > instant(self.settled_at_utc)
            or instant(self.binding.observed_at_utc) < instant(self.started_at_utc)
        ):
            raise ValueError("Invalid terminal checkpoint")
        return self


class SettledTurn(StrictDTO):
    checkpoint: CheckpointPayload


class HostResolution(StrictDTO):
    session_id: str
    turn_id: str | None
    disposition: Literal["abandon_without_replay"] = "abandon_without_replay"
    actor: Reference
    resolved_at_utc: str
    binding: SessionBinding
    note: str = Field(default="", max_length=1024, repr=False)

    _id = field_validator("session_id")(uuid4_text)
    _time = field_validator("resolved_at_utc")(utc_text)

    @field_validator("turn_id")
    @classmethod
    def turn(cls, value: str | None) -> str | None:
        return None if value is None else uuid4_text(value)

    @model_validator(mode="after")
    def relationship(self) -> Self:
        if (
            self.binding.session_id != self.session_id
            or instant(self.binding.observed_at_utc) > instant(self.resolved_at_utc)
            or any(ord(c) < 32 for c in self.note)
        ):
            raise ValueError("Invalid host resolution")
        return self


class AttemptDiagnostics(StrictDTO):
    binding: SessionBinding
    message_start: Annotated[int, Field(ge=0, le=MAX_MESSAGES)]
    fences: tuple[ToolFence, ...] = Field(default=(), max_length=MAX_INVOCATIONS)
    reason: Reason | None = None
    resolution: HostResolution | None = None
    commit_version: Counter | None = None


class TurnRecord(StrictDTO):
    intent: TurnIntent
    state: TurnState
    ended_at_utc: str | None
    settled_seq: Counter | None
    diagnostics: AttemptDiagnostics


class CheckpointReceipt(StrictDTO):
    revision: Revision
    turn_id: str | None
    sha256: Digest
    outcome: Outcome | None

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.revision.head_seq == 0:
            if self.turn_id is not None or self.outcome is not None:
                raise ValueError("Invalid initial receipt")
        elif self.turn_id is None or self.outcome is None:
            raise ValueError("Invalid terminal receipt")
        else:
            uuid4_text(self.turn_id)
        return self


class CheckpointDelta(StrictDTO):
    """Stored V2 record: one settled Turn tail and its exact predecessor receipt.

    CheckpointPayload remains the full, materialized host/runtime interface. A
    receipt hashes stored bytes, never that reconstructed view.
    """

    format_version: Literal[2] = 2
    session_id: str
    turn_id: str | None
    seq: Counter
    binding: SessionBinding
    messages: tuple[StoredMessage, ...] = Field(max_length=MAX_MESSAGES, repr=False)
    message_range: MessageRange
    outcome: Outcome | None
    started_at_utc: str | None
    settled_at_utc: str
    trace: TraceSummary = Field(default_factory=TraceSummary)
    predecessor: CheckpointReceipt | None

    @field_validator("format_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("Invalid format version")
        return value

    @model_validator(mode="after")
    def envelope(self) -> Self:
        # Reuse the existing time, identity, outcome and group-boundary envelope
        # on this tail alone; no historical prefixes are materialized here.
        if self.message_range.end - self.message_range.start != len(self.messages):
            raise ValueError("Invalid delta range")
        values = self.model_dump(
            exclude={"format_version", "predecessor", "message_range"}
        )
        CheckpointPayload.model_validate(
            {
                **values,
                "message_range": MessageRange(start=0, end=len(self.messages)),
            }
        )
        if self.seq == 0:
            if self.predecessor is not None or self.message_range.start != 0:
                raise ValueError("Invalid initial predecessor")
        elif (
            self.predecessor is None
            or self.predecessor.revision.session_id != self.session_id
            or self.predecessor.revision.head_seq + 1 != self.seq
        ):
            raise ValueError("Invalid predecessor identity")
        return self


class RecoveryReport(StrictDTO):
    """One inspection of a Session and its attempt records.

    ``status`` is the readiness answer and already covers ``local_fault``:
    a latched store reports ``recovery_required``, so a check written as
    ``status != "clean"`` needs no separate fault test. ``local_fault`` stays
    because it names *where* the fault is (this process, not the stored row)
    and is used on its own where a notice or release decision needs that
    distinction.
    """

    revision: Revision
    state: SessionState
    status: Literal["clean", "executing", "effects_unknown", "recovery_required"]
    binding: SessionBinding
    checkpoint: CheckpointPayload
    receipt: CheckpointReceipt
    attempts: tuple[TurnRecord, ...]
    local_fault: bool = False

    @property
    def unresolved(self) -> TurnRecord | None:
        return next(
            (t for t in self.attempts if t.state in ("executing", "recovery_required")),
            None,
        )
