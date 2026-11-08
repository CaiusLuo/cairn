"""SQLite-backed authority for Conversations, Tasks and Runs.

This is deliberately a small local store, not a PostgreSQL repository
abstraction.  SQLite transactions give the three identities one writer while
avoiding another full-document JSON rewrite for every state transition.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

from cairn.core.domain import (
    Conversation,
    ConversationSource,
    Run,
    RunEvent,
    RunStatus,
    Task,
    TaskStatus,
    new_conversation_id,
    new_run_id,
    new_task_id,
    utc_now,
    workspace_id_for,
)

SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return default if not value else json.loads(value)


class DomainStore:
    """The only mutating owner of the Conversation/Task/Run tables."""

    def __init__(self, path: Path) -> None:
        raw = path.expanduser()
        if raw.is_symlink():
            raise ValueError("Domain store cannot be a symlink")
        self.path = raw.resolve()
        if self.path.is_symlink():
            raise ValueError("Domain store cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("Unsupported domain store schema")
            self._create_schema(connection)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                source TEXT NOT NULL,
                initial_prompt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                issue_url TEXT,
                worktree TEXT,
                branch TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_conversation_idx
                ON tasks(conversation_id);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                task_id TEXT REFERENCES tasks(task_id),
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                budget_json TEXT NOT NULL,
                trace_ref TEXT,
                session_id TEXT,
                checkpoint_receipt_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_conversation_idx
                ON runs(conversation_id);
            CREATE INDEX IF NOT EXISTS runs_task_idx ON runs(task_id);
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS conversation_requests (
                workspace_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                fingerprint TEXT NOT NULL,
                PRIMARY KEY (workspace_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS run_requests (
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                request_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                fingerprint TEXT NOT NULL,
                PRIMARY KEY (conversation_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS domain_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    # -- Conversation -----------------------------------------------------

    def create_conversation(
        self,
        *,
        workspace: Path | str,
        source: ConversationSource,
        initial_prompt: str,
        conversation_id: str | None = None,
        request_id: str | None = None,
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        workspace_id = workspace_id_for(workspace)
        fingerprint = hashlib.sha256(
            json.dumps([initial_prompt, source.value], ensure_ascii=False).encode()
        ).hexdigest()
        with self._connect() as connection:
            if request_id is not None:
                existing = connection.execute(
                    "SELECT conversation_requests.fingerprint, conversations.* "
                    "FROM conversation_requests JOIN conversations USING(conversation_id) "
                    "WHERE conversation_requests.workspace_id = ? "
                    "AND conversation_requests.request_id = ?",
                    (workspace_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise ValueError(
                            "Conversation request ID belongs to different input"
                        )
                    return self._conversation(existing)
            conversation = Conversation(
                conversation_id=conversation_id or new_conversation_id(),
                workspace_id=workspace_id,
                source=source,
                initial_prompt=initial_prompt,
                created_at=created_at or utc_now(),
                updated_at=created_at or utc_now(),
                metadata=metadata or {},
            )
            connection.execute(
                """
                INSERT INTO conversations
                    (conversation_id, workspace_id, source, initial_prompt,
                     created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    conversation.workspace_id,
                    conversation.source.value,
                    conversation.initial_prompt,
                    conversation.created_at,
                    conversation.updated_at,
                    _json(conversation.metadata),
                ),
            )
            if request_id is not None:
                connection.execute(
                    "INSERT INTO conversation_requests"
                    "(workspace_id, request_id, conversation_id, fingerprint) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        workspace_id,
                        request_id,
                        conversation.conversation_id,
                        fingerprint,
                    ),
                )
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._conversation(row)

    def list_conversations(
        self, *, workspace_id: str | None = None
    ) -> tuple[Conversation, ...]:
        with self._connect() as connection:
            if workspace_id is None:
                rows = connection.execute(
                    "SELECT * FROM conversations ORDER BY created_at, conversation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM conversations WHERE workspace_id = ? "
                    "ORDER BY created_at, conversation_id",
                    (workspace_id,),
                ).fetchall()
        return tuple(self._conversation(row) for row in rows)

    # -- Task --------------------------------------------------------------

    def create_task(
        self,
        *,
        conversation_id: str,
        goal: str,
        title: str = "",
        status: TaskStatus = TaskStatus.TODO,
        task_id: str | None = None,
        issue_url: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Task requires an existing Conversation")
        task = Task(
            task_id=task_id or new_task_id(),
            conversation_id=conversation.conversation_id,
            workspace_id=conversation.workspace_id,
            title=title,
            goal=goal,
            status=status,
            issue_url=issue_url,
            worktree=worktree,
            branch=branch,
            created_at=created_at or utc_now(),
            updated_at=created_at or utc_now(),
            metadata=metadata or {},
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (task_id, conversation_id, workspace_id, title, goal, status,
                     issue_url, worktree, branch, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.conversation_id,
                    task.workspace_id,
                    task.title,
                    task.goal,
                    task.status.value,
                    task.issue_url,
                    task.worktree,
                    task.branch,
                    task.created_at,
                    task.updated_at,
                    _json(task.metadata),
                ),
            )
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else self._task(row)

    def list_tasks(self, *, workspace_id: str | None = None) -> tuple[Task, ...]:
        with self._connect() as connection:
            if workspace_id is None:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at, task_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE workspace_id = ? "
                    "ORDER BY created_at, task_id",
                    (workspace_id,),
                ).fetchall()
        return tuple(self._task(row) for row in rows)

    def list_tasks_for_conversation(self, conversation_id: str) -> tuple[Task, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE conversation_id = ? ORDER BY created_at, task_id",
                (conversation_id,),
            ).fetchall()
        return tuple(self._task(row) for row in rows)

    def set_task_status(
        self, task_id: str, status: TaskStatus, *, note: str | None = None
    ) -> Task:
        return self.update_task(task_id, status=status, note=note)

    def mark_task_done(self, task_id: str, *, note: str | None = None) -> Task:
        return self.set_task_status(task_id, TaskStatus.DONE, note=note)

    def close_task(self, task_id: str, *, note: str | None = None) -> Task:
        return self.set_task_status(task_id, TaskStatus.CLOSED, note=note)

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        title: str | None = None,
        goal: str | None = None,
        issue_url: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        current = self.get_task(task_id)
        if current is None:
            raise ValueError("Task not found")
        values: dict[str, Any] = {
            "updated_at": utc_now(),
            "status": current.status if status is None else status,
            "title": current.title if title is None else title,
            "goal": current.goal if goal is None else goal,
            "issue_url": current.issue_url if issue_url is None else issue_url,
            "worktree": current.worktree if worktree is None else worktree,
            "branch": current.branch if branch is None else branch,
            "metadata": current.metadata if metadata is None else metadata,
        }
        if note:
            values["metadata"] = {**values["metadata"], "last_note": note}
        changed = note is not None or any(
            value is not None
            for value in (status, title, goal, issue_url, worktree, branch, metadata)
        )
        updated = Task.model_validate(
            {
                **current.model_dump(),
                **values,
                "updated_at": values["updated_at"] if changed else current.updated_at,
            }
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks SET title=?, goal=?, status=?, issue_url=?, worktree=?,
                    branch=?, updated_at=?, metadata_json=? WHERE task_id=?
                """,
                (
                    updated.title,
                    updated.goal,
                    updated.status.value,
                    updated.issue_url,
                    updated.worktree,
                    updated.branch,
                    updated.updated_at,
                    _json(updated.metadata),
                    updated.task_id,
                ),
            )
        return updated

    # -- Run ---------------------------------------------------------------

    def create_run(
        self,
        *,
        conversation_id: str,
        status: RunStatus = RunStatus.QUEUED,
        task_id: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
        prompt: str = "",
        request_id: str | None = None,
        source: ConversationSource | None = None,
        mode: str = "conversation",
        result: str | None = None,
        budget: dict[str, Any] | None = None,
        trace_ref: str | None = None,
        session_id: str | None = None,
        checkpoint_receipt: dict[str, Any] | None = None,
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Run:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Run requires an existing Conversation")
        if task_id is not None:
            task = self.get_task(task_id)
            if task is None or task.conversation_id != conversation_id:
                raise ValueError("Run task must belong to its Conversation")
        run = Run(
            run_id=run_id or new_run_id(),
            conversation_id=conversation_id,
            task_id=task_id,
            workspace_id=conversation.workspace_id,
            status=status,
            prompt=prompt,
            request_id=request_id,
            source=source,
            mode=cast(Any, mode),
            result=result,
            error=error,
            budget=budget or {},
            trace_ref=trace_ref,
            session_id=session_id,
            checkpoint_receipt=checkpoint_receipt,
            created_at=created_at or utc_now(),
            updated_at=created_at or utc_now(),
            metadata={
                **(metadata or {}),
                "prompt": prompt,
                "request_id": request_id,
                "source": None if source is None else source.value,
                "mode": mode,
                "result": result,
            },
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, conversation_id, task_id, workspace_id, status, error,
                     budget_json, trace_ref, session_id, checkpoint_receipt_json,
                     created_at, updated_at, ended_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.conversation_id,
                    run.task_id,
                    run.workspace_id,
                    run.status.value,
                    run.error,
                    _json(run.budget),
                    run.trace_ref,
                    run.session_id,
                    None
                    if run.checkpoint_receipt is None
                    else _json(run.checkpoint_receipt),
                    run.created_at,
                    run.updated_at,
                    run.ended_at,
                    _json(run.metadata),
                ),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else self._run(row)

    def list_runs(self, *, workspace_id: str | None = None) -> tuple[Run, ...]:
        with self._connect() as connection:
            if workspace_id is None:
                rows = connection.execute(
                    "SELECT * FROM runs ORDER BY created_at, run_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM runs WHERE workspace_id = ? ORDER BY created_at, run_id",
                    (workspace_id,),
                ).fetchall()
        return tuple(self._run(row) for row in rows)

    def get_run_by_request(self, conversation_id: str, request_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runs.* FROM run_requests JOIN runs USING(run_id) "
                "WHERE run_requests.conversation_id = ? AND run_requests.request_id = ?",
                (conversation_id, request_id),
            ).fetchone()
        return None if row is None else self._run(row)

    def accept_run(
        self,
        *,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        mode: str = "conversation",
        task_id: str | None = None,
    ) -> Run:
        """Atomically accept one request and create its Run, or return the original."""
        import hashlib
        import json as _json_module

        fingerprint = hashlib.sha256(
            _json_module.dumps(
                [prompt, source.value, mode, task_id], ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT runs.*, run_requests.fingerprint FROM run_requests "
                "JOIN runs USING(run_id) WHERE run_requests.conversation_id = ? "
                "AND run_requests.request_id = ?",
                (conversation_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("Request ID belongs to a different input")
                return self._run(existing)
            active = connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id = ? "
                "AND status IN ('queued', 'running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("Conversation already has an active Run")
            conversation = connection.execute(
                "SELECT workspace_id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise ValueError("Run requires an existing Conversation")
            if task_id is not None:
                task = connection.execute(
                    "SELECT conversation_id FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if task is None or task["conversation_id"] != conversation_id:
                    raise ValueError("Run task must belong to its Conversation")
            run = Run(
                conversation_id=conversation_id,
                task_id=task_id,
                workspace_id=cast(str, conversation["workspace_id"]),
                status=RunStatus.QUEUED,
                prompt=prompt,
                request_id=request_id,
                source=source,
                mode=cast(Any, mode),
                metadata={
                    "prompt": prompt,
                    "request_id": request_id,
                    "source": source.value,
                    "mode": mode,
                },
            )
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, conversation_id, task_id, workspace_id, status, error,
                     budget_json, trace_ref, session_id, checkpoint_receipt_json,
                     created_at, updated_at, ended_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.conversation_id,
                    run.task_id,
                    run.workspace_id,
                    run.status.value,
                    run.error,
                    _json(run.budget),
                    run.trace_ref,
                    run.session_id,
                    None,
                    run.created_at,
                    run.updated_at,
                    run.ended_at,
                    _json(run.metadata),
                ),
            )
            connection.execute(
                "INSERT INTO run_requests(conversation_id, request_id, run_id, fingerprint) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, request_id, run.run_id, fingerprint),
            )
        return run

    def list_runs_for_conversation(self, conversation_id: str) -> tuple[Run, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE conversation_id = ? ORDER BY created_at, run_id",
                (conversation_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def list_runs_for_task(self, task_id: str) -> tuple[Run, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at, run_id",
                (task_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        error: str | None = None,
        result: str | None = None,
        task_id: str | None = None,
        budget: dict[str, Any] | None = None,
        trace_ref: str | None = None,
        session_id: str | None = None,
        checkpoint_receipt: dict[str, Any] | None = None,
        ended_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Run:
        current = self.get_run(run_id)
        if current is None:
            raise ValueError("Run not found")
        next_metadata = dict(current.metadata if metadata is None else metadata)
        next_metadata.update(
            {
                "prompt": current.prompt,
                "request_id": current.request_id,
                "source": None if current.source is None else current.source.value,
                "mode": current.mode,
            }
        )
        updated = Run.model_validate(
            {
                **current.model_dump(),
                "status": current.status if status is None else status,
                "error": current.error if error is None else error,
                "result": current.result if result is None else result,
                "task_id": current.task_id if task_id is None else task_id,
                "budget": current.budget if budget is None else budget,
                "trace_ref": current.trace_ref if trace_ref is None else trace_ref,
                "session_id": current.session_id if session_id is None else session_id,
                "checkpoint_receipt": current.checkpoint_receipt
                if checkpoint_receipt is None
                else checkpoint_receipt,
                "ended_at": current.ended_at if ended_at is None else ended_at,
                "metadata": next_metadata,
                "updated_at": utc_now(),
            }
        )
        next_metadata["result"] = updated.result
        updated = updated.model_copy(update={"metadata": next_metadata})
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs SET status=?, error=?, task_id=?, budget_json=?, trace_ref=?,
                    session_id=?, checkpoint_receipt_json=?, updated_at=?, ended_at=?,
                    metadata_json=? WHERE run_id=?
                """,
                (
                    updated.status.value,
                    updated.error,
                    updated.task_id,
                    _json(updated.budget),
                    updated.trace_ref,
                    updated.session_id,
                    None
                    if updated.checkpoint_receipt is None
                    else _json(updated.checkpoint_receipt),
                    updated.updated_at,
                    updated.ended_at,
                    _json(updated.metadata),
                    updated.run_id,
                ),
            )
        return updated

    def append_run_event(
        self, run_id: str, kind: str, data: dict[str, Any] | None = None
    ) -> RunEvent:
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("Run not found")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0]) + 1
            event = RunEvent(
                run_id=run_id,
                sequence=sequence,
                kind=kind,
                data=data or {},
                created_at=utc_now(),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, sequence, kind, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, kind, _json(event.data), event.created_at),
            )
        return event

    def run_event_cursor(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def list_run_events(
        self, run_id: str, after: int = 0, limit: int = 200
    ) -> tuple[RunEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return tuple(
            RunEvent(
                run_id=cast(str, row["run_id"]),
                sequence=int(row["sequence"]),
                kind=cast(str, row["kind"]),
                data=cast(dict[str, Any], _loads(row["data_json"], {})),
                created_at=cast(str, row["created_at"]),
            )
            for row in rows
        )

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
        result: str | None = None,
        ended_at: str | None = None,
    ) -> Run:
        return self.update_run(
            run_id, status=status, error=error, result=result, ended_at=ended_at
        )

    # -- Legacy adapter/migration helpers ---------------------------------

    def has_migration(
        self, source_sha256: str, taskboard_sha256: str | None = None
    ) -> bool:
        with self._connect() as connection:
            rows = dict(
                connection.execute(
                    "SELECT key, value FROM domain_meta "
                    "WHERE key IN ('source_sha256', 'taskboard_sha256')"
                ).fetchall()
            )
        return rows.get("source_sha256") == source_sha256 and rows.get(
            "taskboard_sha256", ""
        ) == (taskboard_sha256 or "")

    def mark_migration(
        self,
        *,
        source_sha256: str,
        taskboard_sha256: str | None = None,
        source: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO domain_meta(key, value) VALUES(?, ?)",
                ("source_sha256", source_sha256),
            )
            connection.execute(
                "INSERT OR REPLACE INTO domain_meta(key, value) VALUES(?, ?)",
                ("taskboard_sha256", taskboard_sha256 or ""),
            )
            connection.execute(
                "INSERT OR REPLACE INTO domain_meta(key, value) VALUES(?, ?)",
                ("source", source),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "conversations": int(
                    connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[
                        0
                    ]
                ),
                "tasks": int(
                    connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                ),
                "runs": int(
                    connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                ),
            }

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            conversation_id=cast(str, row["conversation_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            source=ConversationSource(cast(str, row["source"])),
            initial_prompt=cast(str, row["initial_prompt"]),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            metadata=cast(dict[str, Any], _loads(row["metadata_json"], {})),
        )

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=cast(str, row["task_id"]),
            conversation_id=cast(str, row["conversation_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            title=cast(str, row["title"]),
            goal=cast(str, row["goal"]),
            status=TaskStatus(cast(str, row["status"])),
            issue_url=row["issue_url"],
            worktree=row["worktree"],
            branch=row["branch"],
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            metadata=cast(dict[str, Any], _loads(row["metadata_json"], {})),
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> Run:
        metadata = cast(dict[str, Any], _loads(row["metadata_json"], {}))
        raw_source = metadata.get("source")
        source = (
            None if raw_source is None else ConversationSource(cast(str, raw_source))
        )
        return Run(
            run_id=cast(str, row["run_id"]),
            conversation_id=cast(str, row["conversation_id"]),
            task_id=row["task_id"],
            workspace_id=cast(str, row["workspace_id"]),
            status=RunStatus(cast(str, row["status"])),
            prompt=cast(str, metadata.get("prompt", "")),
            request_id=cast(str | None, metadata.get("request_id")),
            source=source,
            mode=cast(Any, metadata.get("mode", "conversation")),
            result=cast(str | None, metadata.get("result")),
            error=row["error"],
            budget=cast(dict[str, Any], _loads(row["budget_json"], {})),
            trace_ref=row["trace_ref"],
            session_id=row["session_id"],
            checkpoint_receipt=cast(
                dict[str, Any] | None, _loads(row["checkpoint_receipt_json"], None)
            ),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            ended_at=row["ended_at"],
            metadata=cast(dict[str, Any], _loads(row["metadata_json"], {})),
        )


__all__ = ["SCHEMA_VERSION", "DomainStore"]
