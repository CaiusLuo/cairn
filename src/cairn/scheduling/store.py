"""Shared PostgreSQL storage for scheduled jobs and their run facts.

A due job is claimed under a transaction advisory lock before execution and
updated by id afterwards. That keeps a long-running execution from overwriting
jobs added by another session, and prevents two sessions from running the same
due slot. The execution lock is a session advisory lock, held for the whole
task execution across processes.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairn.scheduling.models import ScheduleJob, next_run_after
from cairn.shared.db import Database, DbConnection

SCHEDULE_DIR_ENV = "CAIRN_SCHEDULE_DIR"
STORE_VERSION = 1
_JOBS_KEY = "jobs"
_JOBS_LOCK = "cairn-schedule-jobs"
_EXECUTION_LOCK = int.from_bytes(
    hashlib.sha256(b"cairn-schedule-execution").digest()[:8], "big", signed=True
)


def default_schedule_dir() -> Path:
    override = os.environ.get(SCHEDULE_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "schedule"


def ensure_outside_workspace(state_root: Path, workspace: Path) -> None:
    resolved_state = state_root.resolve()
    resolved_workspace = workspace.resolve()
    if resolved_state == resolved_workspace or resolved_state.is_relative_to(
        resolved_workspace
    ):
        raise ValueError(
            "Schedule state directory must live outside the Agent-writable Workspace"
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExecutionLease:
    """A session advisory lock held on its own connection."""

    def __init__(self, database: Database, key: int = _EXECUTION_LOCK) -> None:
        self.database = database
        self.key = key
        self._connection: DbConnection | None = None

    def acquire(self) -> bool:
        connection = self.database.dedicated("ops")
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (self.key,)
            ).fetchone()
        except BaseException:
            connection.close()
            raise
        if row is None or not bool(row[0]):
            connection.close()
            return False
        self._connection = connection
        return True

    def release(self) -> None:
        if self._connection is None:
            return
        with suppress(Exception):
            self._connection.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
        self._connection.close()
        self._connection = None


class ScheduleStore:
    """Jobs and run history live in the shared ops schema."""

    def __init__(self, database: Database) -> None:
        self.db = database

    @contextmanager
    def _locked(self) -> Iterator[DbConnection]:
        with self.db.connect("ops") as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (_JOBS_LOCK,)
            )
            yield connection

    @contextmanager
    def execution_lock(self) -> Iterator[None]:
        """Serialize execution across sessions and processes."""
        connection = self.db.dedicated("ops")
        try:
            connection.execute("SELECT pg_advisory_lock(%s)", (_EXECUTION_LOCK,))
            yield
        finally:
            connection.close()

    def try_execution_lock(self) -> ExecutionLease | None:
        lease = ExecutionLease(self.db)
        return lease if lease.acquire() else None

    def _load_jobs_unlocked(self, connection: DbConnection) -> tuple[ScheduleJob, ...]:
        row = connection.execute(
            "SELECT payload FROM schedule_state WHERE key = %s", (_JOBS_KEY,)
        ).fetchone()
        if row is None:
            return ()
        raw = row["payload"]
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            raise ValueError("schedule jobs have an unsupported shape/version")
        entries = raw.get("jobs")
        if not isinstance(entries, list):
            raise ValueError("schedule jobs are malformed")
        try:
            jobs = tuple(ScheduleJob.model_validate(item) for item in entries)
        except ValueError:
            raise ValueError("schedule jobs contain an invalid entry") from None
        if len({job.job_id for job in jobs}) != len(jobs):
            raise ValueError("schedule jobs contain duplicate ids")
        return jobs

    def load_jobs(self) -> tuple[ScheduleJob, ...]:
        with self._locked() as connection:
            return self._load_jobs_unlocked(connection)

    def _save_jobs_unlocked(
        self, connection: DbConnection, jobs: tuple[ScheduleJob, ...]
    ) -> None:
        connection.execute(
            "INSERT INTO schedule_state(key, payload) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET payload = excluded.payload",
            (
                _JOBS_KEY,
                _json(
                    {
                        "version": STORE_VERSION,
                        "jobs": [job.model_dump(mode="json") for job in jobs],
                    }
                ),
            ),
        )

    def save_jobs(self, jobs: tuple[ScheduleJob, ...]) -> None:
        with self._locked() as connection:
            self._save_jobs_unlocked(connection, jobs)

    def add_or_get_job(self, job: ScheduleJob) -> tuple[ScheduleJob, bool]:
        """Atomically create a job or return the already-saved exact job."""
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            existing = next((item for item in jobs if item.job_id == job.job_id), None)
            if existing is not None:
                return existing, False
            jobs.append(job)
            self._save_jobs_unlocked(connection, tuple(jobs))
            return job, True

    def add_job(self, job: ScheduleJob) -> None:
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            if any(existing.job_id == job.job_id for existing in jobs):
                raise ValueError(f"duplicate schedule job id: {job.job_id}")
            jobs.append(job)
            self._save_jobs_unlocked(connection, tuple(jobs))

    def remove_job(self, job_id: str) -> bool:
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            kept = [job for job in jobs if job.job_id != job_id]
            if len(kept) == len(jobs):
                return False
            self._save_jobs_unlocked(connection, tuple(kept))
            return True

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(update={"enabled": enabled})
                    self._save_jobs_unlocked(connection, tuple(jobs))
                    return True
            return False

    def claim_due(
        self,
        now: Any,
        *,
        workspace: str | None = None,
        recipient: str | None = None,
        account_id: str | None = None,
        reminder: bool | None = None,
    ) -> tuple[ScheduleJob, str] | None:
        """Claim one due job, optionally restricted to one exact owner.

        Production callers always pass ownership. A legacy job with empty
        workspace/recipient therefore cannot be claimed by a guessed session.
        """
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if not job.enabled or job.next_run_at is None:
                    continue
                if job.next_run_at > now:
                    continue
                if workspace is not None and job.workspace != workspace:
                    continue
                if recipient is not None and job.recipient != recipient:
                    continue
                if account_id is not None and job.account_id != account_id:
                    continue
                if reminder is not None and job.reminder != reminder:
                    continue
                run_key = f"{job.job_id}:{job.next_run_at.isoformat()}"
                claimed = job.model_copy(
                    update={
                        "last_run_at": now,
                        "next_run_at": next_run_after(job, now),
                        "last_status": "running",
                        "last_error": None,
                        "execution_started": False,
                    }
                )
                jobs[index] = claimed
                self._save_jobs_unlocked(connection, tuple(jobs))
                self._append_run_unlocked(
                    connection,
                    {
                        "run_key": run_key,
                        "job_id": job.job_id,
                        "workspace": job.workspace,
                        "recipient": job.recipient,
                        "account_id": job.account_id,
                        "started_at": now.isoformat(),
                        "status": "started",
                    },
                )
                return claimed, run_key
            return None

    def recover_interrupted(
        self,
        now: Any,
        *,
        workspace: str | None,
        recipient: str,
        account_id: str | None,
    ) -> int:
        """Record abandoned runs after the caller acquires the execution lock.

        A caller must hold the cross-process execution lock: a persisted running
        status is not evidence of abandonment while another runner is active.
        """
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            claimed = [
                (index, job)
                for index, job in enumerate(jobs)
                if job.last_status == "running"
                and not job.execution_started
                and (workspace is None or job.workspace == workspace)
                and job.recipient == recipient
                and (account_id is None or job.account_id == account_id)
            ]
            for index, job in claimed:
                jobs[index] = job.model_copy(
                    update={
                        "last_status": "recovered",
                        "last_error": "claimed but not started; retryable",
                        "next_run_at": job.last_run_at or now,
                    }
                )
            pending = [
                (index, job)
                for index, job in enumerate(jobs)
                if job.last_status == "running"
                and job.execution_started
                and (workspace is None or job.workspace == workspace)
                and job.recipient == recipient
                and (account_id is None or job.account_id == account_id)
            ]
            if not pending and not claimed:
                return 0
            runs = self._load_runs_unlocked(connection, limit=2**31 - 1)
            for index, job in pending:
                started_at = job.last_run_at.isoformat() if job.last_run_at else None
                started = next(
                    (
                        row
                        for row in runs
                        if row.get("job_id") == job.job_id
                        and row.get("status") == "started"
                        and row.get("started_at") == started_at
                    ),
                    None,
                )
                run_key = started.get("run_key") if started is not None else None
                if not isinstance(run_key, str) or not run_key:
                    run_key = f"{job.job_id}:interrupted:{started_at or 'unknown'}"
                finished = next(
                    (
                        row
                        for row in runs
                        if row.get("job_id") == job.job_id
                        and row.get("run_key") == run_key
                        and isinstance(row.get("status"), str)
                        and row.get("status") not in {"", "started", "running"}
                        and isinstance(row.get("finished_at"), str)
                    ),
                    None,
                )
                if finished is not None:
                    # A terminal fact may have been committed just before a crash
                    # interrupted the matching job-status update.
                    status = str(finished["status"])
                    saved_error = finished.get("error")
                    error = saved_error if isinstance(saved_error, str) else None
                elif job.reminder:
                    status = "delivery_unknown"
                    error = "reminder delivery interrupted; not retried"
                else:
                    status = "interrupted"
                    error = "execution interrupted; delivery status unknown"
                    self._append_run_unlocked(
                        connection,
                        {
                            "run_key": run_key,
                            "job_id": job.job_id,
                            "workspace": job.workspace,
                            "recipient": job.recipient,
                            "account_id": job.account_id,
                            "finished_at": now.isoformat(),
                            "status": status,
                            "task_id": None,
                            "trace_id": None,
                            "delivered": None,
                            "error": error,
                            "result": "",
                        },
                    )
                jobs[index] = job.model_copy(
                    update={"last_status": status, "last_error": error}
                )
            # The history fact is written first. If the process dies before this
            # update, the next recovery sees that fact and does not append twice.
            self._save_jobs_unlocked(connection, tuple(jobs))
            return len(pending)

    def job_execution_started(self, job_id: str) -> bool:
        with self._locked() as connection:
            jobs = self._load_jobs_unlocked(connection)
            return any(job.job_id == job_id and job.execution_started for job in jobs)

    def restore_claim(self, job_id: str) -> bool:
        """Return a claimed-but-not-started once job to the due queue."""
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(
                        update={
                            "last_status": "recoverable",
                            "last_error": "shutdown before execution; retryable",
                            "next_run_at": job.last_run_at or self._clockless_now(),
                            "execution_started": False,
                        }
                    )
                    self._save_jobs_unlocked(connection, tuple(jobs))
                    return True
            return False

    @staticmethod
    def _clockless_now() -> Any:
        return datetime.now(UTC)

    def mark_execution_started(self, job_id: str) -> bool:
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(
                        update={"execution_started": True, "last_status": "running"}
                    )
                    self._save_jobs_unlocked(connection, tuple(jobs))
                    return True
            return False

    def finish_run(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None,
        record: dict[str, Any],
    ) -> None:
        """Persist the terminal fact before updating the corresponding job."""
        # Two transactions: a failed job-status write must not erase the
        # terminal occurrence fact that recovery relies on.
        with self._locked() as connection:
            self._append_run_unlocked(connection, record)
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(
                        update={"last_status": status, "last_error": error}
                    )
                    self._save_jobs_unlocked(connection, tuple(jobs))
                    break

    def update_job(
        self, job_id: str, *, status: str, error: str | None = None
    ) -> ScheduleJob | None:
        """Update only the claimed job, preserving concurrent add/remove edits."""
        with self._locked() as connection:
            jobs = list(self._load_jobs_unlocked(connection))
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    updated = job.model_copy(
                        update={"last_status": status, "last_error": error}
                    )
                    jobs[index] = updated
                    self._save_jobs_unlocked(connection, tuple(jobs))
                    return updated
            return None

    def load_runs(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Newest-first run history for display."""
        if limit < 1:
            return ()
        with self._locked() as connection:
            return self._load_runs_unlocked(connection, limit)

    def _load_runs_unlocked(
        self, connection: DbConnection, limit: int
    ) -> tuple[dict[str, Any], ...]:
        rows = connection.execute(
            "SELECT record FROM schedule_runs ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()
        return tuple(row["record"] for row in rows if isinstance(row["record"], dict))

    def _append_run_unlocked(
        self, connection: DbConnection, record: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO schedule_runs(record) VALUES (%s)", (_json(record),)
        )

    def append_run(self, record: dict[str, Any]) -> None:
        with self._locked() as connection:
            self._append_run_unlocked(connection, record)

    def load_state(self, key: str) -> dict[str, Any] | None:
        """Read one host-policy document stored beside the job list."""
        with self.db.connect("ops") as connection:
            row = connection.execute(
                "SELECT payload FROM schedule_state WHERE key = %s", (key,)
            ).fetchone()
        if row is None or not isinstance(row["payload"], dict):
            return None
        return row["payload"]

    def save_state(self, key: str, payload: dict[str, Any]) -> None:
        with self._locked() as connection:
            connection.execute(
                "INSERT INTO schedule_state(key, payload) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET payload = excluded.payload",
                (key, _json(payload)),
            )
