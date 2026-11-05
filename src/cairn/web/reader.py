"""Read committed facts for the read-only web surface.

The reader never opens a writer SessionStore and never calls a mutating
TaskBoard method. A missing or malformed source is reported as an unavailable
source, not turned into fabricated success.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairn.core.task_board import TaskBoardItem
from cairn.infra.sessions.models import SessionStoreError, SessionSummary
from cairn.infra.sessions.store import SessionStore
from cairn.infra.task_board.store import TaskBoardStore
from cairn.infra.tools.bash import resolve_tmpdir, resolve_uv_cache_dir
from cairn.infra.tools.credential_env import SECRET_ENV_KEYS

SNAPSHOT_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_status(error: str | None) -> str:
    return "ok" if error is None else error


class SnapshotReader:
    """One bounded, read-only view over the host-selected roots.

    The sequence advances only when the observable Session/Task facts change.
    It is a refresh signal, never a task lifecycle transition.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        sessions_dir: Path,
        tasks_dir: Path,
    ) -> None:
        self.workspace = workspace
        self.sessions_dir = sessions_dir
        self.tasks_dir = tasks_dir
        self._lock = threading.Lock()
        self._sequence = 0
        self._signature: tuple[object, ...] | None = None
        self._last_session_status = "ok"
        self._last_task_status = "ok"

    def _forbidden_roots(self) -> tuple[Path, ...]:
        env = dict(os.environ)
        roots: list[Path] = [self.workspace]
        for candidate in (resolve_tmpdir(env), resolve_uv_cache_dir(env)):
            if candidate is not None:
                roots.append(candidate)
        return tuple(roots)

    def _session_rows(self) -> tuple[list[SessionSummary], str]:
        if not self.sessions_dir.is_dir():
            return [], "missing"
        try:
            store = SessionStore(
                self.sessions_dir,
                mode="inspect",
                forbidden_roots=self._forbidden_roots(),
                sensitive_values=tuple(
                    value
                    for key, value in os.environ.items()
                    if key in SECRET_ENV_KEYS and value
                ),
            )
            try:
                return store.list_sessions(), "ok"
            finally:
                store.close()
        except SessionStoreError as exc:
            return [], "unavailable" if exc.code != "database_missing" else "missing"
        except (OSError, ValueError, RuntimeError):
            return [], "unavailable"

    def _task_items(self) -> tuple[list[TaskBoardItem], str]:
        if not self.tasks_dir.is_dir():
            return [], "missing"
        try:
            return list(TaskBoardStore(self.tasks_dir).load()), "ok"
        except (OSError, ValueError, RuntimeError):
            return [], "unavailable"

    @staticmethod
    def _task_source(task: TaskBoardItem) -> str:
        return task.source.value

    def _session_view(
        self,
        summary: SessionSummary,
        tasks_by_session: dict[str, list[TaskBoardItem]],
    ) -> dict[str, Any]:
        linked = tasks_by_session.get(summary.session_id, [])
        sources = {self._task_source(task) for task in linked}
        # A Session has no stored source field. Infer it only when all linked
        # TaskBoard facts agree; otherwise leave the fact unknown.
        source = next(iter(sources)) if len(sources) == 1 else None
        return {
            "session_id": summary.session_id,
            "short_id": summary.short_id,
            "state": summary.state,
            "title": summary.title,
            "created_at_utc": summary.created_at_utc,
            "updated_at_utc": summary.updated_at_utc,
            "head_seq": summary.head_seq,
            "source": source,
            "task_ids": [task.task_id for task in linked],
            "live": None,
            "agent": None,
        }

    def task_view(self, task: TaskBoardItem) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "project": task.project,
            "source": task.source.value,
            "session_id": task.session_id or None,
            "status": task.status.value,
            "todos": [
                {"todo_id": todo.todo_id, "text": todo.text, "done": todo.done}
                for todo in task.todos
            ],
            "progress": list(task.progress),
            "issue_url": task.issue_url,
            "pr_url": task.pr_url,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "revision": task.revision,
        }

    def snapshot(self) -> dict[str, Any]:
        sessions, session_status = self._session_rows()
        tasks, task_status = self._task_items()
        tasks_by_session: dict[str, list[TaskBoardItem]] = {}
        for task in tasks:
            if task.session_id:
                tasks_by_session.setdefault(task.session_id, []).append(task)
        session_views = [
            self._session_view(summary, tasks_by_session) for summary in sessions
        ]
        task_views = [self.task_view(task) for task in tasks]
        signature = (
            session_status,
            tuple(
                (
                    item["session_id"],
                    item["state"],
                    item["head_seq"],
                    item["updated_at_utc"],
                )
                for item in session_views
            ),
            task_status,
            tuple(
                (
                    item["task_id"],
                    item["status"],
                    item["updated_at"],
                    item["revision"],
                )
                for item in task_views
            ),
        )
        with self._lock:
            if signature != self._signature:
                self._sequence += 1
                self._signature = signature
            sequence = self._sequence
        self._last_session_status = _source_status(session_status)
        self._last_task_status = _source_status(task_status)
        return {
            "version": SNAPSHOT_VERSION,
            "sequence": sequence,
            "generated_at_utc": _now(),
            "read_only": True,
            "source_status": {
                "sessions": self._last_session_status,
                "tasks": self._last_task_status,
            },
            "sessions": session_views,
            "tasks": task_views,
        }

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        if not self.sessions_dir.is_dir():
            return None
        try:
            store = SessionStore(
                self.sessions_dir,
                mode="inspect",
                forbidden_roots=self._forbidden_roots(),
                sensitive_values=tuple(
                    value
                    for key, value in os.environ.items()
                    if key in SECRET_ENV_KEYS and value
                ),
            )
            try:
                report = store.inspect(session_id)
            finally:
                store.close()
        except (SessionStoreError, OSError, ValueError):
            return None
        tasks, _ = self._task_items()
        tasks_by_session: dict[str, list[TaskBoardItem]] = {}
        for task in tasks:
            if task.session_id:
                tasks_by_session.setdefault(task.session_id, []).append(task)
        summary = SessionSummary(
            session_id=report.revision.session_id,
            state=report.state,
            created_at_utc=report.checkpoint.settled_at_utc,
            updated_at_utc=report.checkpoint.settled_at_utc,
            head_seq=report.revision.head_seq,
        )
        messages = [
            {"role": message.role, "content": message.content}
            for message in report.checkpoint.messages
            if message.content is not None and message.role in ("user", "assistant")
        ]
        view = self._session_view(summary, tasks_by_session)
        view["messages"] = messages
        view["recovery_status"] = report.status
        return view
