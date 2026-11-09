"""Persisted development goals and their operator-controlled status."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.conversation.models import utc_now


def new_task_id() -> str:
    return "task-" + uuid4().hex


class TaskStatus(StrEnum):
    """Explicit user/authority state, never derived from a Run result."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    DONE = "done"
    CLOSED = "closed"


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
