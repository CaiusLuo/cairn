"""Single-writer task admission/catalogue; durable execution stays in SessionStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cairn.infra.atomic_json import atomic_write
from cairn.infra.redaction import redact_text, redact_value
from cairn.infra.workspace.lease import WorkspaceLease

RuntimeSource = Literal["cli", "web", "wechat"]
RuntimeStatus = Literal[
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "recovery_required",
]


class RuntimeTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(default_factory=lambda: "task-" + uuid4().hex)
    prompt: str = Field(min_length=1, max_length=32000)
    source: RuntimeSource
    request_id: str | None = None
    status: RuntimeStatus = "queued"
    input_prompt: str | None = Field(default=None, min_length=1, max_length=32000)
    input_receipts: dict[str, str] = Field(default_factory=dict, max_length=1000)
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
            for value in (task.prompt, task.input_prompt or "")
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
