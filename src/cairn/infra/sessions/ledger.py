"""Task ledger mutations for the host-only Session store.

Split from :mod:`cairn.infra.sessions.store` so the Task lifecycle owns its own
module. This mixin is mixed into ``SessionStore`` and relies on the connection,
locking and transaction helpers defined there; it never opens its own
connection and never dispatches work.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Literal

from cairn.infra.sessions.codec import decode_record, decode_storage_checkpoint
from cairn.infra.sessions.models import (
    MAX_RECORD_BYTES,
    CheckpointReceipt,
    RecoveryReport,
    Revision,
    SessionStoreError,
    StrictDTO,
    VersionConflict,
)
from cairn.infra.sessions.task_models import (
    MAX_TASK_BYTES,
    TaskDiagnostic,
    TaskPhase,
    TaskRecord,
    TaskRecoveryReport,
    TaskResultData,
    TaskRevision,
)


class TaskLedgerMixin:
    """Task-record reads, transitions and recovery checks.

    Mixed into ``SessionStore``; the host class supplies the connection,
    locking and transaction helpers declared below. Declaring them here keeps
    the seam explicit and type-checkable instead of implicit.
    """

    # Host class state and helpers (defined by SessionStore).
    mode: Literal["writer", "inspect"]
    sensitive_values: tuple[str, ...]
    _fault: bool
    _owned_tasks: set[tuple[str, str]]

    def _begin(self, expected: Revision | TaskRevision, kind: type[StrictDTO]) -> None:
        raise NotImplementedError

    def _encode(
        self,
        value: StrictDTO,
        *,
        limit: int = MAX_RECORD_BYTES,
        kind: type[StrictDTO] | None = None,
    ) -> bytes:
        raise NotImplementedError

    def _latch(self) -> None:
        raise NotImplementedError

    def _read(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def _report(self, conn: sqlite3.Connection, session_id: str) -> RecoveryReport:
        raise NotImplementedError

    def _row(self, conn: sqlite3.Connection, expected: Revision) -> object:
        raise NotImplementedError

    def _tasks_supported(self, conn: sqlite3.Connection) -> None:
        raise NotImplementedError

    def _write(
        self, *, recovery: bool = False, idle_after: bool = False
    ) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def _receipt_at(
        self, conn: sqlite3.Connection, session: RecoveryReport, seq: int
    ) -> CheckpointReceipt:
        # _report has already verified the entire Session history in this same
        # transaction. Receipts can be historical, but never fabricated heads.
        row = conn.execute(
            "SELECT payload,sha256,format_version FROM checkpoints WHERE session_id=? AND seq=?",
            (session.revision.session_id, seq),
        ).fetchone()
        if row is None:
            raise SessionStoreError("task_checkpoint_missing")
        checkpoint = decode_storage_checkpoint(
            row[0],
            row[1],
            format_version=row[2],
            sensitive_values=self.sensitive_values,
        )
        version = 0
        if seq:
            record = next(
                r for r in session.attempts if r.intent.turn_id == checkpoint.turn_id
            )
            version = record.diagnostics.commit_version or 0
        return CheckpointReceipt(
            revision=Revision(
                session_id=session.revision.session_id, version=version, head_seq=seq
            ),
            turn_id=checkpoint.turn_id,
            sha256=row[1],
            outcome=checkpoint.outcome,
        )

    def _validate_task(
        self, conn: sqlite3.Connection, task: TaskRecord, session: RecoveryReport
    ) -> None:
        base = self._receipt_at(conn, session, task.base_revision.head_seq)
        if (
            task.base_revision.version < base.revision.version
            or task.base_revision.version > session.revision.version
            or task.revision.session_id != session.revision.session_id
        ):
            raise SessionStoreError("invalid_task_relationship")
        if task.checkpoint is not None:
            actual = self._receipt_at(conn, session, task.checkpoint.revision.head_seq)
            if actual != task.checkpoint:
                raise SessionStoreError("task_checkpoint_mismatch")
            turn = next(
                r for r in session.attempts if r.intent.turn_id == actual.turn_id
            )
            if (
                turn.intent.task_id != task.revision.task_id
                or turn.intent.input_digest != task.input_digest
                or turn.intent.base_seq != task.base_revision.head_seq
                or turn.diagnostics.commit_version is None
                or task.base_revision.version >= turn.diagnostics.commit_version
            ):
                raise SessionStoreError("task_turn_mismatch")

    def _task(
        self, conn: sqlite3.Connection, session_id: str, task_id: str
    ) -> tuple[TaskRecord, RecoveryReport]:
        self._tasks_supported(conn)
        session = self._report(conn, session_id)
        row = conn.execute(
            "SELECT * FROM tasks WHERE session_id=? AND task_id=?",
            (session_id, task_id),
        ).fetchone()
        if row is None:
            raise SessionStoreError("task_missing")
        task = decode_record(
            TaskRecord,
            row["record_json"].encode(),
            limit=MAX_TASK_BYTES,
            sensitive_values=self.sensitive_values,
        )
        if (
            (task.revision.session_id, task.revision.task_id, task.revision.version)
            != (row["session_id"], row["task_id"], row["version"])
            or task.phase != row["phase"]
            or task.base_revision.head_seq != row["base_seq"]
            or (None if task.checkpoint is None else task.checkpoint.revision.head_seq)
            != row["checkpoint_seq"]
        ):
            raise SessionStoreError("invalid_task_relationship")
        self._validate_task(conn, task, session)
        return task, session

    def inspect_task(self, session_id: str, task_id: str) -> TaskRecoveryReport:
        """Read facts only. An unfinished Task never acquires a result on restart."""
        with self._read() as conn:
            task, session = self._task(conn, session_id, task_id)
            status: Literal["incomplete", "recovery_required", "result_recorded"] = (
                "result_recorded" if task.phase == "result_recorded" else "incomplete"
            )
            if (
                task.recovery_required
                or self._fault
                or session.state == "recovery_required"
            ):
                status = "recovery_required"
            effects = any(
                r.intent.task_id == task_id
                and r.intent.base_seq == task.base_revision.head_seq
                and r.intent.input_digest == task.input_digest
                and bool(r.diagnostics.fences)
                for r in session.attempts
            )
            return TaskRecoveryReport(
                task=task,
                session_revision=session.revision,
                session_checkpoint=session.receipt,
                status=status,
                external_effects_may_have_occurred=effects,
            )

    def create_task(
        self, expected: Revision, *, task_id: str, input_digest: str
    ) -> TaskRecord:
        self._begin(expected, Revision)
        try:
            task = TaskRecord(
                revision=TaskRevision(
                    session_id=expected.session_id, task_id=task_id, version=0
                ),
                input_digest=input_digest,
                phase="running",
                base_revision=expected,
            )
        except (ValueError, TypeError):
            self._latch()
            raise SessionStoreError("invalid_record") from None
        raw = self._encode(task, limit=MAX_TASK_BYTES, kind=TaskRecord)
        with self._write() as conn:
            self._tasks_supported(conn)
            self._row(conn, expected)
            session = self._report(conn, expected.session_id)
            if session.status != "clean":
                raise SessionStoreError("task_session_not_ready")
            if conn.execute(
                "SELECT 1 FROM tasks WHERE session_id=? AND task_id=?",
                (expected.session_id, task_id),
            ).fetchone():
                raise SessionStoreError("task_exists")
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (
                    expected.session_id,
                    task_id,
                    0,
                    "running",
                    expected.head_seq,
                    None,
                    raw.decode(),
                ),
            )
        return task

    @contextmanager
    def task_execution(self, expected: TaskRevision) -> Iterator[None]:
        """Retain writer ownership through the trusted runtime's Task cleanup.

        This guard records no lifecycle fact and releases no Turn ownership.
        The runtime must leave it only after all work it started has settled.
        """
        if self.mode != "writer":
            raise SessionStoreError("read_only_store")
        self._encode(expected, kind=TaskRevision)
        key = (expected.session_id, expected.task_id)
        if key in self._owned_tasks:
            raise SessionStoreError("task_still_owned")
        with self._read() as conn:
            task, session = self._task_current(conn, expected, "running")
            if (
                session.status != "clean"
                or session.local_fault
                or task.base_revision != session.revision
            ):
                raise SessionStoreError("task_session_not_ready")
        self._owned_tasks.add(key)
        try:
            yield
        finally:
            self._owned_tasks.remove(key)

    def _task_current(
        self, conn: sqlite3.Connection, expected: TaskRevision, phase: TaskPhase
    ) -> tuple[TaskRecord, RecoveryReport]:
        task, session = self._task(conn, expected.session_id, expected.task_id)
        if task.revision != expected:
            raise VersionConflict()
        if task.recovery_required:
            raise SessionStoreError("task_recovery_required")
        if task.phase != phase:
            raise SessionStoreError("invalid_task_transition")
        return task, session

    def _save_task(
        self, conn: sqlite3.Connection, task: TaskRecord, session: RecoveryReport
    ) -> TaskRecord:
        expected = task.revision
        task = task.model_copy(
            update={
                "revision": expected.model_copy(
                    update={"version": expected.version + 1}
                )
            }
        )
        raw = self._encode(task, limit=MAX_TASK_BYTES, kind=TaskRecord)
        task = decode_record(TaskRecord, raw, limit=MAX_TASK_BYTES)
        self._validate_task(conn, task, session)
        changed = conn.execute(
            "UPDATE tasks SET version=?,phase=?,checkpoint_seq=?,record_json=? "
            "WHERE session_id=? AND task_id=? AND version=?",
            (
                task.revision.version,
                task.phase,
                None if task.checkpoint is None else task.checkpoint.revision.head_seq,
                raw.decode(),
                expected.session_id,
                expected.task_id,
                expected.version,
            ),
        ).rowcount
        if changed != 1:
            raise VersionConflict()
        return task

    def commit_task_turn(
        self, expected: TaskRevision, receipt: CheckpointReceipt
    ) -> TaskRecord:
        self._begin(expected, TaskRevision)
        self._encode(receipt, kind=CheckpointReceipt)
        with self._write() as conn:
            task, session = self._task_current(conn, expected, "running")
            if receipt != session.receipt or session.status != "clean":
                raise SessionStoreError("task_checkpoint_mismatch")
            task = task.model_copy(
                update={"phase": "turn_committed", "checkpoint": receipt}
            )
            return self._save_task(conn, task, session)

    def _task_head(self, task: TaskRecord, session: RecoveryReport) -> None:
        if task.checkpoint != session.receipt or session.status != "clean":
            raise SessionStoreError("task_checkpoint_not_current")

    def start_task_inspection(self, expected: TaskRevision) -> TaskRecord:
        self._begin(expected, TaskRevision)
        with self._write() as conn:
            task, session = self._task_current(conn, expected, "turn_committed")
            self._task_head(task, session)
            return self._save_task(
                conn, task.model_copy(update={"phase": "inspecting"}), session
            )

    def record_task_result(
        self, expected: TaskRevision, result: TaskResultData
    ) -> TaskRecord:
        self._begin(expected, TaskRevision)
        raw = self._encode(result, limit=MAX_TASK_BYTES, kind=TaskResultData)
        result = decode_record(TaskResultData, raw, limit=MAX_TASK_BYTES)
        with self._write() as conn:
            task, session = self._task_current(conn, expected, "inspecting")
            self._task_head(task, session)
            return self._save_task(
                conn,
                task.model_copy(update={"phase": "result_recorded", "result": result}),
                session,
            )

    def require_task_recovery(
        self, expected: TaskRevision, diagnostic: TaskDiagnostic
    ) -> TaskRecord:
        self._begin(expected, TaskRevision)
        with self._write(recovery=True) as conn:
            task, session = self._task(conn, expected.session_id, expected.task_id)
            if task.revision != expected:
                raise VersionConflict()
            if task.phase == "result_recorded":
                raise SessionStoreError("invalid_task_transition")
            return self._save_task(
                conn,
                task.model_copy(
                    update={"recovery_required": True, "diagnostic": diagnostic}
                ),
                session,
            )
