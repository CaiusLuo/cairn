"""Authoritative Conversation/Task/Run domain identities.

The domain model separates three lifetimes that were previously represented by
one ``RuntimeTask``:

* ``Conversation`` is a stable, long-lived way to address a dialogue.
* ``Task`` is a durable development goal owned by a conversation.
* ``Run`` is one concrete Agent execution against a conversation and optional
  task.

``DomainStore`` in :mod:`cairn.infra.domain_store` is the write authority for
all three entities.  The existing RuntimeCatalogue remains a read-only legacy
source for migration; it must not be expanded into another mutating task
registry.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def new_conversation_id() -> str:
    return "conv-" + uuid4().hex


def new_task_id() -> str:
    return "task-" + uuid4().hex


def new_run_id() -> str:
    return "run-" + uuid4().hex


def workspace_id_for(workspace: Path | str) -> str:
    """Return a stable workspace identity independent of process cwd."""
    canonical = str(Path(workspace).expanduser().resolve())
    return "ws-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class ConversationSource(StrEnum):
    CLI = "cli"
    WEB = "web"
    WECHAT = "wechat"
    SCHEDULE = "schedule"


class TaskStatus(StrEnum):
    """Explicit user/authority state, never derived from a Run result."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    DONE = "done"
    CLOSED = "closed"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


class Conversation(BaseModel):
    """A durable dialogue identity; it deliberately has no completed state."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    conversation_id: str = Field(default_factory=new_conversation_id)
    workspace_id: str = Field(min_length=1, max_length=100)
    source: ConversationSource
    initial_prompt: str = Field(min_length=1, max_length=32000)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    # Entry-point details and legacy facts that do not belong to a Task/Run.
    metadata: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    """A development goal owned by one Conversation.

    ``status`` is changed only through the authority service.  A Run reaching
    ``completed`` never changes this field to DONE or CLOSED.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    task_id: str = Field(default_factory=new_task_id)
    conversation_id: str = Field(min_length=1, max_length=100)
    workspace_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=300)
    goal: str = Field(min_length=1, max_length=32000)
    status: TaskStatus = TaskStatus.TODO
    issue_url: str | None = Field(default=None, max_length=2000)
    worktree: str | None = Field(default=None, max_length=4096)
    branch: str | None = Field(default=None, max_length=256)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _title_or_goal(self) -> Task:
        if not self.title and not self.goal.strip():
            raise ValueError("Task requires a title or goal")
        return self


class Run(BaseModel):
    """One execution boundary; a Conversation may have many independent Runs."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    run_id: str = Field(default_factory=new_run_id)
    conversation_id: str = Field(min_length=1, max_length=100)
    task_id: str | None = Field(default=None, max_length=100)
    workspace_id: str = Field(min_length=1, max_length=100)
    status: RunStatus = RunStatus.QUEUED
    prompt: str = Field(default="", max_length=32000)
    request_id: str | None = Field(default=None, max_length=200)
    source: ConversationSource | None = None
    mode: Literal["conversation", "development"] = "conversation"
    result: str | None = Field(default=None, max_length=32000)
    error: str | None = Field(default=None, max_length=2000)
    budget: dict[str, Any] = Field(default_factory=dict)
    trace_ref: str | None = Field(default=None, max_length=2000)
    session_id: str | None = Field(default=None, max_length=200)
    # Checkpoint receipts are opaque domain facts; SessionStore remains their
    # schema owner.  They are retained here so a Run can point at its boundary.
    checkpoint_receipt: dict[str, Any] | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    ended_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """A bounded, ordered execution fact for one Run."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    run_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=100)
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


__all__ = [
    "Conversation",
    "ConversationSource",
    "Run",
    "RunEvent",
    "RunStatus",
    "Task",
    "TaskStatus",
    "new_conversation_id",
    "new_run_id",
    "new_task_id",
    "utc_now",
    "workspace_id_for",
]
