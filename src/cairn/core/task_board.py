"""Shared task-board values; display and execution are separate concerns."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskSource(StrEnum):
    LOCAL = "local"
    CLI = "cli"
    WECHAT = "wechat"
    GITHUB = "github"
    WEB = "web"


class TaskBoardStatus(StrEnum):
    TODO = "todo"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskTodo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    todo_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    done: bool = False


class TaskBoardItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    task_id: str = Field(min_length=1, max_length=100)
    project: str = Field(default="", max_length=4096)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    source: TaskSource = TaskSource.LOCAL
    session_id: str = Field(default="", max_length=200)
    status: TaskBoardStatus = TaskBoardStatus.TODO
    progress: tuple[str, ...] = ()
    todos: tuple[TaskTodo, ...] = ()
    issue_url: str | None = Field(default=None, max_length=2000)
    pr_url: str | None = Field(default=None, max_length=2000)
    created_at: str
    updated_at: str
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.status is TaskBoardStatus.COMPLETED and not self.title.strip():
            raise ValueError("completed task requires a title")
        return self
