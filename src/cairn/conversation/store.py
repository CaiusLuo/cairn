"""PostgreSQL-backed authority for Conversations, Tasks and Runs.

One PostgreSQL database gives the three identities one writer while avoiding
another full-document JSON rewrite for every state transition.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cairn.conversation.models import (
    Conversation,
    ConversationSource,
    Run,
    RunEvent,
    RunStatus,
    new_conversation_id,
    new_run_id,
    utc_now,
    workspace_id_for,
)
from cairn.development.models import (
    HandoffRecord,
    HandoffReviewRecord,
    HandoffReviewStatus,
    HandoffStatus,
    Task,
    TaskStatus,
    new_task_id,
)
from cairn.shared.db import Database, DbConnection, Row

SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def channel_binding_digest(
    platform: str, connection_id: str, sender_id: str, conversation_binding: str
) -> str:
    """Stable identity for one external channel conversation."""
    raw = f"channel-binding:{platform}:{connection_id}:{sender_id}\0{conversation_binding}"
    return hashlib.sha256(raw.encode()).hexdigest()


class DomainStore:
    """The only mutating owner of the Conversation/Task/Run tables."""

    def __init__(self, database: Database) -> None:
        self.db = database

    @contextmanager
    def _connect(self) -> Iterator[DbConnection]:
        with self.db.connect("core") as connection:
            yield connection

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
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"conversation-request:{workspace_id}:{request_id}",),
                )
                existing = connection.execute(
                    "SELECT conversation_requests.fingerprint, conversations.* "
                    "FROM conversation_requests JOIN conversations USING(conversation_id) "
                    "WHERE conversation_requests.workspace_id = %s "
                    "AND conversation_requests.request_id = %s",
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
                VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                    "VALUES (%s, %s, %s, %s)",
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
                "SELECT * FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._conversation(row)

    @staticmethod
    def _wechat_digests(account_id: str, sender_id: str) -> tuple[str, str]:
        return (
            hashlib.sha256(("wechat-account:" + account_id).encode()).hexdigest(),
            hashlib.sha256(("wechat-sender:" + sender_id).encode()).hexdigest(),
        )

    def get_wechat_conversation(
        self, *, workspace: Path | str, account_id: str, sender_id: str
    ) -> Conversation | None:
        if not account_id or not sender_id:
            return None
        workspace_id = workspace_id_for(workspace)
        account_digest, sender_digest = self._wechat_digests(account_id, sender_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversations.* FROM wechat_bindings "
                "JOIN conversations USING(conversation_id) "
                "WHERE wechat_bindings.workspace_id = %s "
                "AND wechat_bindings.account_digest = %s "
                "AND wechat_bindings.sender_digest = %s",
                (workspace_id, account_digest, sender_digest),
            ).fetchone()
        return self._conversation(row) if row is not None else None

    def is_wechat_binding(
        self,
        conversation_id: str,
        *,
        account_id: str,
        sender_id: str,
    ) -> bool:
        """Whether this conversation belongs to the exact WeChat account/sender.

        The binding stores only hashed identity digests, so a caller that knows
        the trusted account and owner identity can verify ownership without
        recovering any other sender's identifier.
        """
        if not conversation_id or not account_id or not sender_id:
            return False
        account_digest, sender_digest = self._wechat_digests(account_id, sender_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM wechat_bindings WHERE conversation_id = %s "
                "AND account_digest = %s AND sender_digest = %s LIMIT 1",
                (conversation_id, account_digest, sender_digest),
            ).fetchone()
        return row is not None

    def get_or_create_wechat_conversation(
        self,
        *,
        workspace: Path | str,
        account_id: str,
        sender_id: str,
        initial_prompt: str,
    ) -> Conversation:
        """Bind a WeChat identity to one Conversation atomically."""
        if not account_id or not sender_id:
            raise ValueError("WeChat binding requires account and sender identity")
        if not initial_prompt.strip():
            raise ValueError("WeChat binding requires an initial prompt")
        workspace_id = workspace_id_for(workspace)
        account_digest, sender_digest = self._wechat_digests(account_id, sender_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversations.* FROM wechat_bindings "
                "JOIN conversations USING(conversation_id) "
                "WHERE wechat_bindings.workspace_id = %s "
                "AND wechat_bindings.account_digest = %s "
                "AND wechat_bindings.sender_digest = %s",
                (workspace_id, account_digest, sender_digest),
            ).fetchone()
            if row is not None:
                return self._conversation(row)
            conversation = Conversation(
                conversation_id=new_conversation_id(),
                workspace_id=workspace_id,
                source=ConversationSource.WECHAT,
                initial_prompt=initial_prompt,
                metadata={"wechat_binding": True},
            )
            connection.execute(
                "INSERT INTO conversations "
                "(conversation_id, workspace_id, source, initial_prompt, "
                "created_at, updated_at, metadata_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
            inserted = connection.execute(
                "INSERT INTO wechat_bindings "
                "(workspace_id, account_digest, sender_digest, conversation_id, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    workspace_id,
                    account_digest,
                    sender_digest,
                    conversation.conversation_id,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
            if inserted.rowcount == 0:
                connection.execute(
                    "DELETE FROM conversations WHERE conversation_id = %s",
                    (conversation.conversation_id,),
                )
                row = connection.execute(
                    "SELECT conversations.* FROM wechat_bindings "
                    "JOIN conversations USING(conversation_id) "
                    "WHERE wechat_bindings.workspace_id = %s "
                    "AND wechat_bindings.account_digest = %s "
                    "AND wechat_bindings.sender_digest = %s",
                    (workspace_id, account_digest, sender_digest),
                ).fetchone()
                if row is None:
                    raise RuntimeError("WeChat binding race lost its winner")
                return self._conversation(row)
        return conversation

    def get_channel_conversation(
        self,
        *,
        workspace: Path | str,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
    ) -> Conversation | None:
        if not all((platform, connection_id, sender_id, conversation_binding)):
            return None
        workspace_id = workspace_id_for(workspace)
        digest = channel_binding_digest(
            platform, connection_id, sender_id, conversation_binding
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversations.* FROM channel_bindings "
                "JOIN conversations USING(conversation_id) "
                "WHERE channel_bindings.workspace_id = %s "
                "AND channel_bindings.connection_id = %s "
                "AND channel_bindings.binding_digest = %s",
                (workspace_id, connection_id, digest),
            ).fetchone()
        return self._conversation(row) if row is not None else None

    def get_or_create_channel_conversation(
        self,
        *,
        workspace: Path | str,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
        initial_prompt: str,
    ) -> Conversation:
        """Bind one external channel conversation to one Cairn Conversation."""
        if not all((platform, connection_id, sender_id, conversation_binding)):
            raise ValueError("Channel binding requires platform and identities")
        if not initial_prompt.strip():
            raise ValueError("Channel binding requires an initial prompt")
        workspace_id = workspace_id_for(workspace)
        digest = channel_binding_digest(
            platform, connection_id, sender_id, conversation_binding
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversations.* FROM channel_bindings "
                "JOIN conversations USING(conversation_id) "
                "WHERE channel_bindings.workspace_id = %s "
                "AND channel_bindings.connection_id = %s "
                "AND channel_bindings.binding_digest = %s",
                (workspace_id, connection_id, digest),
            ).fetchone()
            if row is not None:
                return self._conversation(row)
            conversation = Conversation(
                conversation_id=new_conversation_id(),
                workspace_id=workspace_id,
                source=ConversationSource.CHANNEL,
                initial_prompt=initial_prompt,
                metadata={
                    "channel_binding": True,
                    "platform": platform,
                    "connection_id": connection_id,
                },
            )
            connection.execute(
                "INSERT INTO conversations "
                "(conversation_id, workspace_id, source, initial_prompt, "
                "created_at, updated_at, metadata_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
            inserted = connection.execute(
                "INSERT INTO channel_bindings "
                "(workspace_id, connection_id, platform, binding_digest, "
                "conversation_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    workspace_id,
                    connection_id,
                    platform,
                    digest,
                    conversation.conversation_id,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
            if inserted.rowcount == 0:
                connection.execute(
                    "DELETE FROM conversations WHERE conversation_id = %s",
                    (conversation.conversation_id,),
                )
                row = connection.execute(
                    "SELECT conversations.* FROM channel_bindings "
                    "JOIN conversations USING(conversation_id) "
                    "WHERE channel_bindings.workspace_id = %s "
                    "AND channel_bindings.connection_id = %s "
                    "AND channel_bindings.binding_digest = %s",
                    (workspace_id, connection_id, digest),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Channel binding race lost its winner")
                return self._conversation(row)
        return conversation

    def set_conversation_archived(
        self, conversation_id: str, *, archived: bool
    ) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            connection.execute(
                "UPDATE conversations SET archived_at = %s WHERE conversation_id = %s",
                (utc_now() if archived else None, conversation_id),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        assert row is not None
        return self._conversation(row)

    def set_conversation_title(
        self, conversation_id: str, title: str | None
    ) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            connection.execute(
                "UPDATE conversations SET title = %s WHERE conversation_id = %s",
                (title, conversation_id),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        assert row is not None
        return self._conversation(row)

    def delete_conversation(self, conversation_id: str) -> None:
        """Remove a conversation with its runs, receipts and bindings.

        Tasks are durable work items with worktrees attached, so a
        conversation that owns one must be archived instead of deleted.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if connection.execute(
                "SELECT 1 FROM tasks WHERE conversation_id = %s LIMIT 1",
                (conversation_id,),
            ).fetchone():
                raise ValueError("Conversation owns development tasks")
            connection.execute(
                "DELETE FROM handoff_reviews WHERE run_id IN "
                "(SELECT run_id FROM development_handoffs WHERE conversation_id = %s)",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM development_handoffs WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM run_events WHERE run_id IN "
                "(SELECT run_id FROM runs WHERE conversation_id = %s)",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM run_requests WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM runs WHERE conversation_id = %s", (conversation_id,)
            )
            connection.execute(
                "DELETE FROM wechat_bindings WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM channel_bindings WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM conversation_requests WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM conversations WHERE conversation_id = %s",
                (conversation_id,),
            )

    def list_conversations(
        self, *, workspace_id: str | None = None, include_archived: bool = False
    ) -> tuple[Conversation, ...]:
        conditions: list[str] = []
        parameters: list[str] = []
        if workspace_id is not None:
            conditions.append("workspace_id = %s")
            parameters.append(workspace_id)
        if not include_archived:
            conditions.append("archived_at IS NULL")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations" + where + " ORDER BY COALESCE("
                "(SELECT MAX(runs.created_at) FROM runs "
                "WHERE runs.conversation_id = conversations.conversation_id),"
                " conversations.updated_at) DESC, conversation_id DESC",
                parameters,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                "SELECT * FROM tasks WHERE task_id = %s", (task_id,)
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
                    "SELECT * FROM tasks WHERE workspace_id = %s "
                    "ORDER BY created_at, task_id",
                    (workspace_id,),
                ).fetchall()
        return tuple(self._task(row) for row in rows)

    def list_tasks_for_conversation(self, conversation_id: str) -> tuple[Task, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE conversation_id = %s ORDER BY created_at, task_id",
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
                UPDATE tasks SET title=%s, goal=%s, status=%s, issue_url=%s, worktree=%s,
                    branch=%s, updated_at=%s, metadata_json=%s WHERE task_id=%s
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

    def project_task_execution(
        self,
        task_id: str,
        *,
        execution_metadata: dict[str, Any],
        status: TaskStatus | None = None,
        worktree: str | None = None,
        branch: str | None = None,
    ) -> Task | None:
        """Merge execution projection into one Task under a SQLite write lock.

        The read, sequence check and update are one transaction.  User-authored
        fields and metadata are copied from the row read inside that transaction,
        so a concurrent DONE/CLOSED or title/metadata edit cannot be overwritten.
        """
        incoming_sequence = execution_metadata.get("execution_sequence")
        if type(incoming_sequence) is not int or incoming_sequence < 0:
            raise ValueError("Execution projection requires a non-negative sequence")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is None:
                return None
            current = self._task(row)
            metadata = dict(current.metadata)
            prior_sequence = metadata.get("execution_sequence")
            if type(prior_sequence) is int and incoming_sequence < prior_sequence:
                return current
            merged = {**metadata, **execution_metadata}
            next_status = current.status
            if status is not None and current.status not in {
                TaskStatus.DONE,
                TaskStatus.CLOSED,
            }:
                next_status = status
            next_worktree = current.worktree if worktree is None else worktree
            next_branch = current.branch if branch is None else branch
            changed = (
                next_status != current.status
                or next_worktree != current.worktree
                or next_branch != current.branch
                or merged != metadata
            )
            if not changed:
                return current
            updated = Task.model_validate(
                {
                    **current.model_dump(),
                    "status": next_status,
                    "worktree": next_worktree,
                    "branch": next_branch,
                    "metadata": merged,
                    "updated_at": utc_now(),
                }
            )
            connection.execute(
                """
                UPDATE tasks SET status=%s, worktree=%s, branch=%s, updated_at=%s,
                    metadata_json=%s WHERE task_id=%s
                """,
                (
                    updated.status.value,
                    updated.worktree,
                    updated.branch,
                    updated.updated_at,
                    _json(updated.metadata),
                    task_id,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            connection.execute(
                "UPDATE conversations SET updated_at = %s WHERE conversation_id = %s",
                (run.created_at, conversation_id),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = %s", (run_id,)
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
                    "SELECT * FROM runs WHERE workspace_id = %s ORDER BY created_at, run_id",
                    (workspace_id,),
                ).fetchall()
        return tuple(self._run(row) for row in rows)

    def recent_runs(self, workspace_id: str) -> tuple[Run, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE workspace_id = %s "
                "ORDER BY created_at DESC, run_id DESC LIMIT 100",
                (workspace_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def review_activity_between(
        self,
        *,
        workspace_id: str,
        start: str,
        end: str,
    ) -> tuple[tuple[Task, ...], tuple[Run, ...]]:
        """Read Tasks and Runs touched in one explicit UTC half-open window.

        This is a read-only projection for review generation. It never changes
        business status, execution status or history; the returned Pydantic
        models are detached snapshots.
        """
        if not workspace_id:
            raise ValueError("workspace_id must not be blank")
        if not start or not end or start >= end:
            raise ValueError("activity window is invalid")
        with self._connect() as connection:
            task_rows = connection.execute(
                "SELECT * FROM tasks WHERE workspace_id = %s "
                "AND ((created_at >= %s AND created_at < %s) "
                "OR (updated_at >= %s AND updated_at < %s)) "
                "ORDER BY updated_at, task_id",
                (workspace_id, start, end, start, end),
            ).fetchall()
            run_rows = connection.execute(
                "SELECT * FROM runs WHERE workspace_id = %s "
                "AND ((created_at >= %s AND created_at < %s) "
                "OR (updated_at >= %s AND updated_at < %s) "
                "OR (ended_at >= %s AND ended_at < %s)) "
                "ORDER BY created_at, run_id",
                (workspace_id, start, end, start, end, start, end),
            ).fetchall()
        return (
            tuple(self._task(row) for row in task_rows),
            tuple(self._run(row) for row in run_rows),
        )

    def get_run_by_request(self, conversation_id: str, request_id: str) -> Run | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runs.* FROM run_requests JOIN runs USING(run_id) "
                "WHERE run_requests.conversation_id = %s AND run_requests.request_id = %s",
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
        model_id: str | None = None,
        status: RunStatus = RunStatus.QUEUED,
        metadata: dict[str, Any] | None = None,
    ) -> Run:
        """Atomically accept one request and create its Run, or return the original."""
        import hashlib
        import json as _json_module

        request_facts: list[object] = [prompt, source.value, mode, task_id]
        if model_id is not None:
            request_facts.append(model_id)
        if metadata is not None:
            request_facts.append(metadata)
        fingerprint = hashlib.sha256(
            _json_module.dumps(request_facts, ensure_ascii=False).encode()
        ).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"run-request:{conversation_id}:{request_id}",),
            )
            existing = connection.execute(
                "SELECT runs.*, run_requests.fingerprint FROM run_requests "
                "JOIN runs USING(run_id) WHERE run_requests.conversation_id = %s "
                "AND run_requests.request_id = %s",
                (conversation_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("Request ID belongs to a different input")
                return self._run(existing)
            conversation = connection.execute(
                "SELECT workspace_id FROM conversations WHERE conversation_id = %s "
                "FOR UPDATE",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise ValueError("Run requires an existing Conversation")
            recovery = connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id = %s AND status = %s LIMIT 1",
                (conversation_id, RunStatus.RECOVERY_REQUIRED.value),
            ).fetchone()
            if recovery is not None:
                raise ValueError("Conversation has a Run requiring recovery review")
            active = connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id = %s "
                "AND status IN ('queued', 'running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("Conversation already has an active Run")
            if task_id is not None:
                task = connection.execute(
                    "SELECT conversation_id FROM tasks WHERE task_id = %s", (task_id,)
                ).fetchone()
                if task is None or task["conversation_id"] != conversation_id:
                    raise ValueError("Run task must belong to its Conversation")
            run = Run(
                conversation_id=conversation_id,
                task_id=task_id,
                workspace_id=cast(str, conversation["workspace_id"]),
                status=status,
                prompt=prompt,
                request_id=request_id,
                source=source,
                mode=cast(Any, mode),
                metadata={
                    **(metadata or {}),
                    "prompt": prompt,
                    "request_id": request_id,
                    "source": source.value,
                    "mode": mode,
                    **({"model_id": model_id} if model_id is not None else {}),
                },
            )
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, conversation_id, task_id, workspace_id, status, error,
                     budget_json, trace_ref, session_id, checkpoint_receipt_json,
                     created_at, updated_at, ended_at, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                "VALUES (%s, %s, %s, %s)",
                (conversation_id, request_id, run.run_id, fingerprint),
            )
        return run

    def list_runs_for_conversation(self, conversation_id: str) -> tuple[Run, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE conversation_id = %s ORDER BY created_at, run_id",
                (conversation_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def list_runs_for_task(self, task_id: str) -> tuple[Run, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE task_id = %s ORDER BY created_at, run_id",
                (task_id,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def bind_run_session(self, run_id: str, session_id: str) -> Run:
        """Bind one durable Session identity to a Run under a write transaction.

        The first successful Session creation wins. Repeating the same binding
        is idempotent; a different Session can never silently replace it.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._run(row)
            if current.session_id is not None:
                if current.session_id != session_id:
                    raise ValueError("Run is already bound to another Session")
                return current
            updated = current.model_copy(
                update={"session_id": session_id, "updated_at": utc_now()}
            )
            connection.execute(
                "UPDATE runs SET session_id = %s, updated_at = %s "
                "WHERE run_id = %s AND session_id IS NULL",
                (session_id, updated.updated_at, run_id),
            )
            return updated

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
                UPDATE runs SET status=%s, error=%s, task_id=%s, budget_json=%s, trace_ref=%s,
                    session_id=%s, checkpoint_receipt_json=%s, updated_at=%s, ended_at=%s,
                    metadata_json=%s WHERE run_id=%s
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
                    "SELECT 1 FROM runs WHERE run_id = %s", (run_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("Run not found")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = %s",
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
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, sequence, kind, _json(event.data), event.created_at),
            )
        return event

    def prune_run_diagnostics(
        self, *, kinds: tuple[str, ...], before: str, keep: int
    ) -> None:
        """Drop old diagnostic rows without changing any Run's high-water cursor."""
        placeholders = ", ".join("%s" for _ in kinds)
        with self._connect() as connection:
            connection.execute(
                f"""
                DELETE FROM run_events WHERE id IN (
                    SELECT event_row FROM (
                        SELECT event.id AS event_row, event.created_at,
                            ROW_NUMBER() OVER (
                                ORDER BY event.created_at DESC, event.id DESC
                            ) AS recency
                        FROM run_events AS event
                        WHERE event.kind IN ({placeholders})
                          AND event.sequence < (
                              SELECT MAX(tail.sequence) FROM run_events AS tail
                              WHERE tail.run_id = event.run_id
                          )
                    ) AS pruned WHERE created_at < %s OR recency > %s
                )
                """,
                (*kinds, before, keep),
            )

    def run_event_cursor(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def list_run_events(
        self, run_id: str, after: int = 0, limit: int = 200
    ) -> tuple[RunEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = %s AND sequence > %s "
                "ORDER BY sequence LIMIT %s",
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

    # -- Development Handoff ------------------------------------------------

    def begin_handoff(
        self,
        *,
        run_id: str,
        conversation_id: str,
        request_id: str,
        prompt: str,
        source: str,
        history_origins: tuple[dict[str, Any], ...] = (),
    ) -> HandoffRecord:
        """Persist the stable intent before either external store is touched."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["conversation_id"] != conversation_id:
                raise ValueError("Handoff Run belongs to another Conversation")
            existing = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s", (run_id,)
            ).fetchone()
            if existing is not None:
                current = self._handoff(existing)
                if (
                    current.conversation_id != conversation_id
                    or current.request_id != request_id
                    or current.prompt != prompt
                    or current.source != source
                ):
                    raise ValueError("Handoff intent belongs to different input")
                if current.history_origins != tuple(history_origins):
                    if not current.history_origins and history_origins:
                        now = utc_now()
                        connection.execute(
                            "UPDATE development_handoffs SET history_origins_json=%s, "
                            "updated_at=%s WHERE run_id=%s",
                            (_json(list(history_origins)), now, run_id),
                        )
                        current = current.model_copy(
                            update={
                                "history_origins": tuple(history_origins),
                                "updated_at": now,
                            }
                        )
                    else:
                        raise ValueError("Handoff history origins changed")
                return current
            record = HandoffRecord(
                run_id=run_id,
                conversation_id=conversation_id,
                request_id=request_id,
                prompt=prompt,
                source=source,
                history_origins=history_origins,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO development_handoffs
                    (run_id, conversation_id, request_id, status, runtime_task_id,
                     domain_task_id, prompt, source, diagnostic,
                     history_origins_json, created_at, updated_at, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.run_id,
                    record.conversation_id,
                    record.request_id,
                    record.status.value,
                    record.runtime_task_id,
                    record.domain_task_id,
                    record.prompt,
                    record.source,
                    record.diagnostic,
                    _json(list(record.history_origins)),
                    record.created_at,
                    record.updated_at,
                    _json(record.metadata),
                ),
            )
            return record

    def get_handoff(self, run_id: str) -> HandoffRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s", (run_id,)
            ).fetchone()
        return None if row is None else self._handoff(row)

    def list_incomplete_handoffs(self) -> tuple[HandoffRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM development_handoffs "
                "WHERE status NOT IN (%s, %s) ORDER BY created_at, run_id",
                (HandoffStatus.COMPLETED.value, HandoffStatus.ABANDONED.value),
            ).fetchall()
        return tuple(self._handoff(row) for row in rows)

    def update_handoff(
        self,
        run_id: str,
        *,
        status: HandoffStatus | None = None,
        runtime_task_id: str | None = None,
        domain_task_id: str | None = None,
        diagnostic: str | None = None,
        history_origins: tuple[dict[str, Any], ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HandoffRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._handoff(row)
            next_status = current.status if status is None else HandoffStatus(status)
            if (
                current.status is HandoffStatus.COMPLETED
                and next_status is not HandoffStatus.COMPLETED
            ):
                raise ValueError("Completed handoff cannot be reopened")
            for label, existing, incoming in (
                ("RuntimeTask", current.runtime_task_id, runtime_task_id),
                ("Domain Task", current.domain_task_id, domain_task_id),
            ):
                if (
                    existing is not None
                    and incoming is not None
                    and existing != incoming
                ):
                    raise ValueError(f"Handoff is already bound to another {label}")
            updated = current.model_copy(
                update={
                    "status": next_status,
                    "runtime_task_id": current.runtime_task_id
                    if runtime_task_id is None
                    else runtime_task_id,
                    "domain_task_id": current.domain_task_id
                    if domain_task_id is None
                    else domain_task_id,
                    "diagnostic": current.diagnostic
                    if diagnostic is None
                    else diagnostic,
                    "history_origins": history_origins
                    if history_origins
                    else current.history_origins,
                    "metadata": {**current.metadata, **metadata}
                    if metadata is not None
                    else current.metadata,
                    "updated_at": utc_now(),
                }
            )
            connection.execute(
                """
                UPDATE development_handoffs SET status=%s, runtime_task_id=%s,
                    domain_task_id=%s, diagnostic=%s, history_origins_json=%s,
                    updated_at=%s, metadata_json=%s
                WHERE run_id=%s
                """,
                (
                    updated.status.value,
                    updated.runtime_task_id,
                    updated.domain_task_id,
                    updated.diagnostic,
                    _json(list(updated.history_origins)),
                    updated.updated_at,
                    _json(updated.metadata),
                    run_id,
                ),
            )
            return updated

    def begin_handoff_review(
        self,
        run_id: str,
        *,
        request_id: str,
        action: str,
        expected_updated_at: str,
        note: str,
    ) -> tuple[HandoffRecord, HandoffReviewRecord]:
        """Atomically claim one review while holding the handoff write lock."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            handoff_row = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if handoff_row is None:
                raise KeyError(run_id)
            current = self._handoff(handoff_row)
            review_row = connection.execute(
                "SELECT * FROM handoff_reviews WHERE request_id = %s", (request_id,)
            ).fetchone()
            if review_row is not None:
                review = self._handoff_review(review_row)
                if (
                    review.run_id != run_id
                    or review.action != action
                    or review.expected_updated_at != expected_updated_at
                    or review.note != note
                ):
                    raise ValueError("Review request belongs to different input")
                if review.status is HandoffReviewStatus.COMPLETED:
                    if current.status in {
                        HandoffStatus.COMPLETED,
                        HandoffStatus.ABANDONED,
                    }:
                        return current, review
                    raise ValueError("Review request state is incomplete")
                if review.status is HandoffReviewStatus.FAILED:
                    return current, review
                raise ValueError("Handoff review is already in progress")
            if current.status in {HandoffStatus.COMPLETED, HandoffStatus.ABANDONED}:
                raise ValueError("Handoff is already terminal")
            if current.status is HandoffStatus.REVIEWING:
                raise ValueError("Handoff review is already in progress")
            if current.updated_at != expected_updated_at:
                raise ValueError("handoff_version_conflict")
            cursor = connection.execute(
                "UPDATE development_handoffs SET status=%s, updated_at=%s "
                "WHERE run_id=%s AND status=%s AND updated_at=%s",
                (
                    HandoffStatus.REVIEWING.value,
                    now,
                    run_id,
                    current.status.value,
                    current.updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("handoff_version_conflict")
            updated = current.model_copy(
                update={"status": HandoffStatus.REVIEWING, "updated_at": now}
            )
            review = HandoffReviewRecord(
                request_id=request_id,
                run_id=run_id,
                action=action,
                expected_updated_at=expected_updated_at,
                note=note,
                status=HandoffReviewStatus.IN_PROGRESS,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO handoff_reviews
                    (request_id, run_id, action, expected_updated_at, note, status,
                     result_status, diagnostic, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review.request_id,
                    review.run_id,
                    review.action,
                    review.expected_updated_at,
                    review.note,
                    review.status.value,
                    review.result_status,
                    review.diagnostic,
                    review.created_at,
                    review.updated_at,
                ),
            )
            return updated, review

    def finish_handoff_review(
        self,
        run_id: str,
        request_id: str,
        *,
        final_status: HandoffStatus,
    ) -> HandoffRecord:
        if final_status not in {HandoffStatus.COMPLETED, HandoffStatus.ABANDONED}:
            raise ValueError("Invalid final handoff status")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review_row = connection.execute(
                "SELECT * FROM handoff_reviews WHERE request_id = %s", (request_id,)
            ).fetchone()
            if review_row is None:
                raise KeyError(request_id)
            review = self._handoff_review(review_row)
            handoff_row = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s", (run_id,)
            ).fetchone()
            if handoff_row is None:
                raise KeyError(run_id)
            current = self._handoff(handoff_row)
            if review.status is HandoffReviewStatus.COMPLETED:
                return current
            if review.status is HandoffReviewStatus.FAILED:
                raise ValueError("Review request already failed")
            if review.run_id != run_id or current.status is not HandoffStatus.REVIEWING:
                raise ValueError("handoff_review_state_conflict")
            now = utc_now()
            updated = current.model_copy(
                update={
                    "status": final_status,
                    "updated_at": now,
                    "metadata": {
                        **current.metadata,
                        "review_request_id": request_id,
                        "review_action": review.action,
                        "review_note": review.note,
                    },
                }
            )
            cursor = connection.execute(
                "UPDATE development_handoffs SET status=%s, updated_at=%s, "
                "metadata_json=%s WHERE run_id=%s AND status=%s",
                (
                    updated.status.value,
                    updated.updated_at,
                    _json(updated.metadata),
                    run_id,
                    HandoffStatus.REVIEWING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("handoff_review_state_conflict")
            connection.execute(
                "UPDATE handoff_reviews SET status=%s, result_status=%s, updated_at=%s "
                "WHERE request_id=%s",
                (
                    HandoffReviewStatus.COMPLETED.value,
                    final_status.value,
                    now,
                    request_id,
                ),
            )
            return updated

    def fail_handoff_review(
        self, run_id: str, request_id: str, *, diagnostic: str
    ) -> HandoffRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            review_row = connection.execute(
                "SELECT * FROM handoff_reviews WHERE request_id = %s", (request_id,)
            ).fetchone()
            if review_row is None:
                raise KeyError(request_id)
            review = self._handoff_review(review_row)
            handoff_row = connection.execute(
                "SELECT * FROM development_handoffs WHERE run_id = %s", (run_id,)
            ).fetchone()
            if handoff_row is None:
                raise KeyError(run_id)
            current = self._handoff(handoff_row)
            if review.status is HandoffReviewStatus.COMPLETED:
                return current
            if review.status is HandoffReviewStatus.FAILED:
                return current
            if review.run_id != run_id or current.status is not HandoffStatus.REVIEWING:
                raise ValueError("handoff_review_state_conflict")
            now = utc_now()
            updated = current.model_copy(
                update={
                    "status": HandoffStatus.NEEDS_REVIEW,
                    "diagnostic": diagnostic,
                    "updated_at": now,
                }
            )
            cursor = connection.execute(
                "UPDATE development_handoffs SET status=%s, diagnostic=%s, updated_at=%s "
                "WHERE run_id=%s AND status=%s",
                (
                    updated.status.value,
                    updated.diagnostic,
                    updated.updated_at,
                    run_id,
                    HandoffStatus.REVIEWING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("handoff_review_state_conflict")
            connection.execute(
                "UPDATE handoff_reviews SET status=%s, diagnostic=%s, updated_at=%s "
                "WHERE request_id=%s",
                (
                    HandoffReviewStatus.FAILED.value,
                    diagnostic,
                    now,
                    request_id,
                ),
            )
            return updated

    def recover_interrupted_handoff_reviews(self) -> int:
        """Turn reviews interrupted by a crash into an explicit review state."""
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM handoff_reviews WHERE status = %s",
                (HandoffReviewStatus.IN_PROGRESS.value,),
            ).fetchall()
            for row in rows:
                review = self._handoff_review(row)
                handoff_row = connection.execute(
                    "SELECT * FROM development_handoffs WHERE run_id = %s",
                    (review.run_id,),
                ).fetchone()
                if handoff_row is None:
                    continue
                current = self._handoff(handoff_row)
                if current.status is not HandoffStatus.REVIEWING:
                    continue
                now = utc_now()
                connection.execute(
                    "UPDATE development_handoffs SET status=%s, diagnostic=%s, "
                    "updated_at=%s WHERE run_id=%s AND status=%s",
                    (
                        HandoffStatus.NEEDS_REVIEW.value,
                        "review_interrupted",
                        now,
                        review.run_id,
                        HandoffStatus.REVIEWING.value,
                    ),
                )
                connection.execute(
                    "UPDATE handoff_reviews SET status=%s, diagnostic=%s, updated_at=%s "
                    "WHERE request_id=%s",
                    (
                        HandoffReviewStatus.FAILED.value,
                        "review_interrupted",
                        now,
                        review.request_id,
                    ),
                )
                recovered += 1
        return recovered

    def create_task_if_absent(
        self,
        *,
        conversation_id: str,
        task_id: str,
        title: str,
        goal: str,
        status: TaskStatus = TaskStatus.TODO,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Create one Domain Task once; a retried handoff reuses the same ID."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = %s FOR UPDATE",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            existing = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s", (task_id,)
            ).fetchone()
            if existing is not None:
                task = self._task(existing)
                if task.conversation_id != conversation_id:
                    raise ValueError("Task already belongs to another Conversation")
                return task
            task = Task(
                task_id=task_id,
                conversation_id=conversation_id,
                workspace_id=cast(str, conversation["workspace_id"]),
                title=title,
                goal=goal,
                status=status,
                metadata=metadata or {},
            )
            connection.execute(
                """
                INSERT INTO tasks
                    (task_id, conversation_id, workspace_id, title, goal, status,
                     issue_url, worktree, branch, created_at, updated_at, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    def associate_run_task(self, run_id: str, task_id: str) -> Run:
        """Associate one Run with its Domain Task first-writer-wins."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = self._run(row)
            if current.task_id is not None:
                if current.task_id != task_id:
                    raise ValueError("Run is already associated with another Task")
                return current
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            if task_row["conversation_id"] != current.conversation_id:
                raise ValueError("Run Task must belong to its Conversation")
            updated = current.model_copy(
                update={"task_id": task_id, "updated_at": utc_now()}
            )
            connection.execute(
                "UPDATE runs SET task_id=%s, updated_at=%s "
                "WHERE run_id=%s AND task_id IS NULL",
                (task_id, updated.updated_at, run_id),
            )
            return updated

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
                "INSERT INTO domain_meta(key, value) VALUES(%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                ("source_sha256", source_sha256),
            )
            connection.execute(
                "INSERT INTO domain_meta(key, value) VALUES(%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                ("taskboard_sha256", taskboard_sha256 or ""),
            )
            connection.execute(
                "INSERT INTO domain_meta(key, value) VALUES(%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
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
    def _conversation(row: Row) -> Conversation:
        return Conversation(
            conversation_id=cast(str, row["conversation_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            source=ConversationSource(cast(str, row["source"])),
            initial_prompt=cast(str, row["initial_prompt"]),
            title=cast(str | None, row["title"]),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            archived_at=cast(str | None, row["archived_at"]),
            metadata=cast(dict[str, Any], _loads(row["metadata_json"], {})),
        )

    @staticmethod
    def _task(row: Row) -> Task:
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
    def _handoff(row: Row) -> HandoffRecord:
        return HandoffRecord(
            run_id=cast(str, row["run_id"]),
            conversation_id=cast(str, row["conversation_id"]),
            request_id=cast(str, row["request_id"]),
            status=HandoffStatus(cast(str, row["status"])),
            runtime_task_id=cast(str | None, row["runtime_task_id"]),
            domain_task_id=cast(str | None, row["domain_task_id"]),
            prompt=cast(str, row["prompt"]),
            source=cast(str, row["source"]),
            diagnostic=cast(str | None, row["diagnostic"]),
            history_origins=tuple(
                cast(list[dict[str, Any]], _loads(row["history_origins_json"], []))
            ),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            metadata=cast(dict[str, Any], _loads(row["metadata_json"], {})),
        )

    @staticmethod
    def _handoff_review(row: Row) -> HandoffReviewRecord:
        return HandoffReviewRecord(
            request_id=cast(str, row["request_id"]),
            run_id=cast(str, row["run_id"]),
            action=cast(str, row["action"]),
            expected_updated_at=cast(str, row["expected_updated_at"]),
            note=cast(str, row["note"]),
            status=HandoffReviewStatus(cast(str, row["status"])),
            result_status=cast(str | None, row["result_status"]),
            diagnostic=cast(str | None, row["diagnostic"]),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
        )

    @staticmethod
    def _run(row: Row) -> Run:
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
