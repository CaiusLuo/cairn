"""Shared task admission/catalogue; durable execution stays in SessionStore."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cairn.agent.session.models import CheckpointReceipt
from cairn.shared.db import Database, DbConnection
from cairn.shared.redaction import redact_text, redact_value

RuntimeSource = Literal["cli", "web", "wechat", "channel", "schedule"]
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


class ChannelPrincipal(BaseModel):
    """Trusted inbound identity attached to one channel Run.

    The adapter derives ``sender_id`` from the platform update, and the host
    records the role from the connection's owner/allowlist. Message text never
    contributes to this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    connection_id: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=20)
    sender_id: str = Field(min_length=1, max_length=200)
    role: Literal["owner", "allowed"]
    conversation_binding: str = Field(min_length=1, max_length=200)


class RuntimeAttachment(BaseModel):
    """A host-stored file bound to exactly one Run's model context."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=0, le=25 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(default_factory=lambda: "task-" + uuid4().hex)
    prompt: str = Field(min_length=1, max_length=32000)
    source: RuntimeSource
    model_id: str | None = None
    # A full selection is persisted with the task/run so recovery and traces
    # never have to guess which provider served a model id.
    provider_name: str | None = None
    model_group: str | None = None
    delivery_recipient: str | None = Field(default=None, max_length=256)
    delivery_account: str | None = Field(default=None, max_length=256)
    channel_principal: ChannelPrincipal | None = None
    mode: Literal["conversation", "development"] = "development"
    conversation_key: str | None = Field(default=None, max_length=200)
    development_prompt: str | None = Field(default=None, min_length=1, max_length=32000)
    attachments: tuple[RuntimeAttachment, ...] = Field(default=(), max_length=5)
    history_origins: tuple[CheckpointReceipt, ...] = Field(default=(), max_length=8)
    # Bounded, permissionless text recovered from durable Run facts when no
    # Session transcript covers those turns. The host supplies it; prompt text
    # can never grant tools or authorization.
    historical_context: str = Field(default="", max_length=64000)
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


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CatalogueData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    tasks: dict[str, RuntimeTask] = Field(default_factory=dict)
    events: dict[str, list[RuntimeEvent]] = Field(default_factory=dict)


class RuntimeCatalogue:
    def __init__(
        self,
        database: Database,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.db = database
        self.sensitive_values = tuple(s for s in sensitive_values if s)
        with self.db.connect("ops") as connection:
            self.data = self._load_unlocked(connection)

    @contextmanager
    def _locked(self) -> Iterator[DbConnection]:
        with self.db.connect("ops") as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("cairn-runtime-catalogue",),
            )
            yield connection

    def _load_unlocked(self, connection: DbConnection) -> CatalogueData:
        row = connection.execute(
            "SELECT payload FROM runtime_state WHERE key = 'catalogue'"
        ).fetchone()
        data = (
            CatalogueData()
            if row is None
            else CatalogueData.model_validate(row["payload"])
        )
        for key, task in data.tasks.items():
            events = data.events.get(key, [])
            if (
                key != task.task_id
                or [e.sequence for e in events] != list(range(1, task.sequence + 1))
                or any(e.task_id != key for e in events)
            ):
                raise ValueError("Invalid runtime event history")
        return data

    def _save_unlocked(self, connection: DbConnection, data: CatalogueData) -> None:
        connection.execute(
            "INSERT INTO runtime_state(key, payload) VALUES ('catalogue', %s) "
            "ON CONFLICT (key) DO UPDATE SET payload = excluded.payload",
            (_json(data.model_dump(mode="json")),),
        )

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
        with self._locked() as connection:
            current = self._load_unlocked(connection)
            previous = current.tasks.get(task.task_id)
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
            ) or (previous is None and len(current.tasks) >= 1000):
                raise RuntimeError("Runtime catalogue capacity reached")
            updated = task.model_copy(update={"sequence": seq})
            candidate = current.model_copy(deep=True)
            candidate.tasks[task.task_id] = updated
            candidate.events.setdefault(task.task_id, []).append(
                RuntimeEvent(
                    task_id=task.task_id,
                    sequence=seq,
                    kind=kind,
                    data=redact_value(data or {}, self.sensitive_values),
                )
            )
            self._save_unlocked(connection, candidate)
            self.data = candidate
        return updated

    def close(self) -> None:
        pass
