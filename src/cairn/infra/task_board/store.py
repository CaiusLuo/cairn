"""Host-private JSON store for the shared task board.

The store owns only facts. It never runs a task, formats a panel or reads model
context. A process lock makes read-modify-write updates safe across CLI/WeChat/Web.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from cairn.core.task_board import (
    TASK_DETAIL_MAX_LENGTH,
    TaskBoardItem,
    TaskBoardStatus,
    TaskSource,
    TaskTodo,
)
from cairn.infra.atomic_json import atomic_write as _atomic_write

TASK_BOARD_DIR_ENV = "CAIRN_TASK_BOARD_DIR"
STORE_VERSION = 1
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0


class TaskBoardBusy(RuntimeError):
    """The shared board lock did not become available within the bounded wait."""


class TaskBoardValidationError(ValueError):
    """A mutation was rejected before it could be written."""


class TaskBoardRevisionConflict(RuntimeError):
    """The task changed after the caller read its revision."""

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"task {task_id} revision {actual} does not match expected {expected}"
        )


def default_task_board_dir() -> Path:
    override = os.environ.get(TASK_BOARD_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "tasks"


def ensure_outside_workspace(state_root: Path, workspace: Path) -> None:
    state = state_root.resolve()
    root = workspace.resolve()
    if state == root or state.is_relative_to(root):
        raise ValueError("Task board directory must live outside the Workspace")


class TaskBoardStore:
    def __init__(
        self, root: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    ) -> None:
        self.root = root
        self.path = root / "tasks.json"
        self.lock_path = root / "tasks.lock"
        if lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive")
        self.lock_timeout = lock_timeout

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            with suppress(OSError):
                self.lock_path.chmod(0o600)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TaskBoardBusy("task board is busy; retry later") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> tuple[TaskBoardItem, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError):
            raise ValueError("task board is unreadable") from None
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            raise ValueError("task board has an unsupported shape/version")
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("task board has an invalid tasks list")
        try:
            return tuple(TaskBoardItem.model_validate(item) for item in tasks)
        except ValueError:
            raise ValueError("task board contains an invalid task") from None

    def load(self) -> tuple[TaskBoardItem, ...]:
        with self._locked():
            return self._load_unlocked()

    def _save_unlocked(self, tasks: tuple[TaskBoardItem, ...]) -> None:
        _atomic_write(
            self.path,
            {
                "version": STORE_VERSION,
                "tasks": [task.model_dump(mode="json") for task in tasks],
            },
        )

    def project_execution(self, item: TaskBoardItem) -> TaskBoardItem:
        """Update a host-owned presentation view; never dispatch execution."""
        with self._locked():
            items = list(self._load_unlocked())
            for index, current in enumerate(items):
                if current.task_id == item.task_id:
                    if current.project != item.project:
                        raise TaskBoardValidationError(
                            "Execution projection project mismatch"
                        )
                    item = item.model_copy(
                        update={
                            "created_at": current.created_at,
                            "revision": current.revision + 1,
                        }
                    )
                    items[index] = item
                    break
            else:
                items.append(item)
            self._save_unlocked(tuple(items))
            return item

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def create(
        self,
        *,
        title: str,
        description: str = "",
        project: str = "",
        source: TaskSource = TaskSource.LOCAL,
        session_id: str = "",
        issue_url: str | None = None,
    ) -> TaskBoardItem:
        now = self._now()
        task = TaskBoardItem(
            task_id="task-" + uuid.uuid4().hex[:12],
            project=project,
            title=title.strip(),
            description=description.strip(),
            source=source,
            session_id=session_id,
            status=TaskBoardStatus.TODO,
            issue_url=issue_url,
            created_at=now,
            updated_at=now,
        )
        with self._locked():
            tasks = self._load_unlocked()
            self._save_unlocked((*tasks, task))
        return task

    def update(
        self,
        task_id: str,
        *,
        status: TaskBoardStatus | None = None,
        title: str | None = None,
        description: str | None = None,
        session_id: str | None = None,
        progress: str | None = None,
        issue_url: str | None = None,
        pr_url: str | None = None,
    ) -> TaskBoardItem | None:
        with self._locked():
            tasks = list(self._load_unlocked())
            for index, task in enumerate(tasks):
                if task.task_id == task_id:
                    updates: dict[str, Any] = {
                        "updated_at": self._now(),
                        "revision": task.revision + 1,
                    }
                    for key, value in (
                        ("status", status),
                        ("title", title),
                        ("description", description),
                        ("session_id", session_id),
                        ("issue_url", issue_url),
                        ("pr_url", pr_url),
                    ):
                        if value is not None:
                            updates[key] = value
                    if progress:
                        progress_text = progress.strip()
                        if len(progress_text) > TASK_DETAIL_MAX_LENGTH:
                            raise TaskBoardValidationError(
                                f"progress must be at most {TASK_DETAIL_MAX_LENGTH} characters"
                            )
                        updates["progress"] = (*task.progress, progress_text)
                    try:
                        updated = TaskBoardItem.model_validate(
                            {**task.model_dump(), **updates}
                        )
                    except ValueError:
                        raise TaskBoardValidationError("invalid task update") from None
                    tasks[index] = updated
                    self._save_unlocked(tuple(tasks))
                    return updated
        return None

    def update_status_if_revision(
        self,
        task_id: str,
        expected_revision: int,
        status: TaskBoardStatus,
    ) -> TaskBoardItem | None:
        """Move one task status only when the caller's revision still matches."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise TaskBoardValidationError(
                "expected_revision must be a positive integer"
            )
        with self._locked():
            tasks = list(self._load_unlocked())
            for index, task in enumerate(tasks):
                if task.task_id != task_id:
                    continue
                if task.revision != expected_revision:
                    raise TaskBoardRevisionConflict(
                        task.task_id, expected_revision, task.revision
                    )
                if task.status is status:
                    return task
                try:
                    updated = TaskBoardItem.model_validate(
                        {
                            **task.model_dump(),
                            "status": status,
                            "updated_at": self._now(),
                            "revision": task.revision + 1,
                        }
                    )
                except ValueError:
                    raise TaskBoardValidationError("invalid task status") from None
                tasks[index] = updated
                self._save_unlocked(tuple(tasks))
                return updated
        return None

    def add_todo(self, task_id: str, text: str) -> TaskBoardItem | None:
        with self._locked():
            tasks = list(self._load_unlocked())
            for index, task in enumerate(tasks):
                if task.task_id == task_id:
                    todo_text = text.strip()
                    if len(todo_text) > TASK_DETAIL_MAX_LENGTH:
                        raise TaskBoardValidationError(
                            f"todo must be at most {TASK_DETAIL_MAX_LENGTH} characters"
                        )
                    todo = TaskTodo(
                        todo_id="todo-" + uuid.uuid4().hex[:10],
                        text=todo_text,
                    )
                    try:
                        updated = TaskBoardItem.model_validate(
                            {
                                **task.model_dump(),
                                "todos": (*task.todos, todo),
                                "updated_at": self._now(),
                                "revision": task.revision + 1,
                            }
                        )
                    except ValueError:
                        raise TaskBoardValidationError("invalid todo") from None
                    tasks[index] = updated
                    self._save_unlocked(tuple(tasks))
                    return updated
        return None

    def set_todo_done(
        self, task_id: str, todo_id: str, done: bool
    ) -> TaskBoardItem | None:
        with self._locked():
            tasks = list(self._load_unlocked())
            for index, task in enumerate(tasks):
                if task.task_id == task_id:
                    try:
                        todos = tuple(
                            TaskTodo.model_validate({**todo.model_dump(), "done": done})
                            if todo.todo_id == todo_id
                            else todo
                            for todo in task.todos
                        )
                    except ValueError:
                        raise TaskBoardValidationError("invalid todo state") from None
                    if not any(todo.todo_id == todo_id for todo in task.todos):
                        return None
                    if todos == task.todos:
                        return task
                    updated = TaskBoardItem.model_validate(
                        {
                            **task.model_dump(),
                            "todos": todos,
                            "updated_at": self._now(),
                            "revision": task.revision + 1,
                        }
                    )
                    tasks[index] = updated
                    self._save_unlocked(tuple(tasks))
                    return updated
        return None
