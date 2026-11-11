"""Single-writer task admission/catalogue; durable execution stays in SessionStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cairn.agent.session.models import CheckpointReceipt
from cairn.agent.workspace.lease import WorkspaceLease
from cairn.shared.atomic_json import atomic_write
from cairn.shared.redaction import redact_text, redact_value

RuntimeSource = Literal["cli", "web", "wechat", "schedule"]
RuntimeStatus = Literal[
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "recovery_required",
]

INPUT_EXCERPT_LIMIT = 1000
INPUT_DIAGNOSTIC_LIMIT = 500


def fit_input_text(
    text: str, *, limit: int, sensitive_values: tuple[str, ...]
) -> tuple[str, bool]:
    """Redact before truncating so a credential cannot survive at the cut."""
    redacted = redact_text(text, sensitive_values)
    return redacted[:limit], len(redacted) > limit


def _fit_optional_input_text(
    text: str | None, *, limit: int, sensitive_values: tuple[str, ...]
) -> str | None:
    return (
        None
        if text is None
        else fit_input_text(text, limit=limit, sensitive_values=sensitive_values)[0]
    )


class TaskInput(BaseModel):
    """Durable receipt for one appended input and its execution evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=200)
    source: RuntimeSource
    status: RuntimeStatus = "queued"
    settled_sequence: int | None = Field(default=None, ge=0)
    result_excerpt: str | None = Field(default=None, max_length=INPUT_EXCERPT_LIMIT)
    result_truncated: bool = False
    diagnostic: str | None = Field(default=None, max_length=INPUT_DIAGNOSTIC_LIMIT)
    session_id: str | None = None
    worktree: str | None = None


class RuntimeTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(default_factory=lambda: "task-" + uuid4().hex)
    prompt: str = Field(min_length=1, max_length=32000)
    source: RuntimeSource
    model_id: str | None = None
    delivery_recipient: str | None = Field(default=None, max_length=256)
    delivery_account: str | None = Field(default=None, max_length=256)
    mode: Literal["conversation", "development"] = "development"
    conversation_key: str | None = Field(default=None, max_length=200)
    development_prompt: str | None = Field(default=None, min_length=1, max_length=32000)
    history_origins: tuple[CheckpointReceipt, ...] = Field(default=(), max_length=8)
    request_id: str | None = None
    status: RuntimeStatus = "queued"
    resume_requested: bool = False
    input_id: str | None = Field(default=None, max_length=200)
    input_prompt: str | None = Field(default=None, min_length=1, max_length=32000)
    input_receipts: dict[str, str] = Field(default_factory=dict, max_length=1000)
    inputs: tuple[TaskInput, ...] = Field(default=(), max_length=50)
    session_id: str | None = None
    worktree: str | None = None
    branch: str | None = None
    result: str | None = None
    diagnostic: str | None = None
    recovery_note: str | None = None
    sequence: int = Field(default=0, ge=0, le=11000)
    attempts: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    sequence: int
    kind: str
    data: dict[str, Any]


class CatalogueData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    tasks: dict[str, RuntimeTask] = Field(default_factory=dict)
    events: dict[str, list[RuntimeEvent]] = Field(default_factory=dict)


class RuntimeCatalogue:
    def __init__(
        self,
        root: Path,
        *,
        forbidden_roots: tuple[Path, ...],
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.sensitive_values = tuple(s for s in sensitive_values if s)
        raw = root.absolute()
        if raw != raw.resolve() or any(
            raw == p.resolve()
            or raw.is_relative_to(p.resolve())
            or p.resolve().is_relative_to(raw)
            for p in forbidden_roots
        ):
            raise ValueError("Runtime state must be private and outside writable roots")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / "runtime.json"
        if self.path.is_symlink() or (root / "owner.lock").is_symlink():
            raise ValueError("Runtime state cannot use symlinks")
        self.lease = WorkspaceLease(root / "owner.lock")
        if not self.lease.acquire():
            raise RuntimeError("Another runtime owns this state directory")
        try:
            self.data = (
                CatalogueData.model_validate_json(self.path.read_bytes())
                if self.path.exists()
                else CatalogueData()
            )
            for key, task in self.data.tasks.items():
                events = self.data.events.get(key, [])
                if (
                    key != task.task_id
                    or [e.sequence for e in events] != list(range(1, task.sequence + 1))
                    or any(e.task_id != key for e in events)
                ):
                    raise ValueError("Invalid runtime event history")
        except BaseException:
            self.lease.release()
            raise

    def record(
        self, task: RuntimeTask, kind: str, data: dict[str, Any] | None = None
    ) -> RuntimeTask:
        if any(
            secret in value
            for value in (
                task.prompt,
                task.input_prompt or "",
                task.development_prompt or "",
            )
            for secret in self.sensitive_values
        ):
            raise ValueError("Prompt contains a protected credential")
        task = task.model_copy(
            update={
                key: redact_text(value, self.sensitive_values)
                for key in ("result", "diagnostic", "recovery_note")
                if isinstance(value := getattr(task, key), str)
            }
        )
        if task.inputs:
            # Validate the redacted text so a growing replacement cannot leave
            # persisted state that a restart refuses to load.
            task = task.model_copy(
                update={
                    "inputs": tuple(
                        TaskInput.model_validate(
                            {
                                **item.model_dump(),
                                "result_excerpt": _fit_optional_input_text(
                                    item.result_excerpt,
                                    limit=INPUT_EXCERPT_LIMIT,
                                    sensitive_values=self.sensitive_values,
                                ),
                                "diagnostic": _fit_optional_input_text(
                                    item.diagnostic,
                                    limit=INPUT_DIAGNOSTIC_LIMIT,
                                    sensitive_values=self.sensitive_values,
                                ),
                            }
                        )
                        for item in task.inputs
                    )
                }
            )
        task = task.model_copy(
            update={"metrics": redact_value(task.metrics, self.sensitive_values)}
        )
        previous = self.data.tasks.get(task.task_id)
        seq = 1 if previous is None else previous.sequence + 1
        if (
            seq > 10000
            and kind
            not in {
                "interrupted",
                "failed",
                "paused",
                "completed",
                "reconciled",
                "cancelled",
            }
        ) or (previous is None and len(self.data.tasks) >= 1000):
            raise RuntimeError("Runtime catalogue capacity reached")
        updated = task.model_copy(update={"sequence": seq})
        candidate = self.data.model_copy(deep=True)
        candidate.tasks[task.task_id] = updated
        candidate.events.setdefault(task.task_id, []).append(
            RuntimeEvent(
                task_id=task.task_id,
                sequence=seq,
                kind=kind,
                data=redact_value(data or {}, self.sensitive_values),
            )
        )
        atomic_write(self.path, candidate.model_dump(mode="json"))
        self.data = candidate
        return updated

    def close(self) -> None:
        self.lease.release()
