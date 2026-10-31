"""Host-private JSON storage with an exclusive claim/update protocol.

A due job is claimed under a file lock before execution and updated by id
afterwards. That keeps a long-running execution from overwriting jobs added by
another session, and prevents two sessions from running the same due slot.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from cairn.core.schedule import ScheduleJob, next_run_after

SCHEDULE_DIR_ENV = "CAIRN_SCHEDULE_DIR"
STORE_VERSION = 1


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


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


class ExecutionLease:
    """Held file descriptor for a non-blocking execution lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        self.handle = self.path.open("a+", encoding="utf-8")
        with suppress(OSError):
            self.path.chmod(0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is not None:
            with suppress(OSError):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class ScheduleStore:
    """One JSON document for jobs; append-only JSONL for run facts."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_path = root / "jobs.json"
        self.runs_path = root / "runs.jsonl"
        self.lock_path = root / "schedule.lock"
        self.execution_lock_path = root / "runner.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            with suppress(OSError):
                self.lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def execution_lock(self) -> Iterator[None]:
        """Serialize execution across sessions and processes.

        This lock is held for the whole task execution, not just the claim. A
        second process therefore cannot run a different due job concurrently.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        with self.execution_lock_path.open("a+", encoding="utf-8") as handle:
            with suppress(OSError):
                self.execution_lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def try_execution_lock(self) -> ExecutionLease | None:
        lease = ExecutionLease(self.execution_lock_path)
        return lease if lease.acquire() else None

    def _load_jobs_unlocked(self) -> tuple[ScheduleJob, ...]:
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError):
            raise ValueError("schedule jobs are unreadable") from None
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
        with self._locked():
            return self._load_jobs_unlocked()

    def _save_jobs_unlocked(self, jobs: tuple[ScheduleJob, ...]) -> None:
        _atomic_write(
            self.jobs_path,
            {
                "version": STORE_VERSION,
                "jobs": [job.model_dump(mode="json") for job in jobs],
            },
        )

    def save_jobs(self, jobs: tuple[ScheduleJob, ...]) -> None:
        with self._locked():
            self._save_jobs_unlocked(jobs)

    def add_job(self, job: ScheduleJob) -> None:
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            if any(existing.job_id == job.job_id for existing in jobs):
                raise ValueError(f"duplicate schedule job id: {job.job_id}")
            jobs.append(job)
            self._save_jobs_unlocked(tuple(jobs))

    def remove_job(self, job_id: str) -> bool:
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            kept = [job for job in jobs if job.job_id != job_id]
            if len(kept) == len(jobs):
                return False
            self._save_jobs_unlocked(tuple(kept))
            return True

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(update={"enabled": enabled})
                    self._save_jobs_unlocked(tuple(jobs))
                    return True
            return False

    def claim_due(
        self,
        now: Any,
        *,
        workspace: str | None = None,
        recipient: str | None = None,
        account_id: str | None = None,
    ) -> tuple[ScheduleJob, str] | None:
        """Claim one due job, optionally restricted to one exact owner.

        Production callers always pass ownership. A legacy job with empty
        workspace/recipient therefore cannot be claimed by a guessed session.
        """
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
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
                run_key = f"{job.job_id}:{job.next_run_at.isoformat()}"
                claimed = job.model_copy(
                    update={
                        "last_run_at": now,
                        "next_run_at": next_run_after(job, now),
                        "last_status": "running",
                        "last_error": None,
                    }
                )
                jobs[index] = claimed
                self._save_jobs_unlocked(tuple(jobs))
                self._append_run_unlocked(
                    {
                        "run_key": run_key,
                        "job_id": job.job_id,
                        "workspace": job.workspace,
                        "recipient": job.recipient,
                        "account_id": job.account_id,
                        "started_at": now.isoformat(),
                        "status": "started",
                    }
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
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            pending = [
                (index, job)
                for index, job in enumerate(jobs)
                if job.last_status == "running"
                and (workspace is None or job.workspace == workspace)
                and job.recipient == recipient
                and (account_id is None or job.account_id == account_id)
            ]
            if not pending:
                return 0
            runs = self.load_runs(limit=2**31 - 1)
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
                    # A terminal fact may have been fsynced just before a crash
                    # interrupted the matching job-status update.
                    status = str(finished["status"])
                    saved_error = finished.get("error")
                    error = saved_error if isinstance(saved_error, str) else None
                else:
                    status = "interrupted"
                    error = "execution interrupted; delivery status unknown"
                    self._append_run_unlocked(
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
                        }
                    )
                jobs[index] = job.model_copy(
                    update={"last_status": status, "last_error": error}
                )
            # The history fact is written first. If the process dies before this
            # update, the next recovery sees that fact and does not append twice.
            self._save_jobs_unlocked(tuple(jobs))
            return len(pending)

    def finish_run(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None,
        record: dict[str, Any],
    ) -> None:
        """Persist the terminal fact before updating the corresponding job."""
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            self._append_run_unlocked(record)
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    jobs[index] = job.model_copy(
                        update={"last_status": status, "last_error": error}
                    )
                    self._save_jobs_unlocked(tuple(jobs))
                    break

    def update_job(
        self, job_id: str, *, status: str, error: str | None = None
    ) -> ScheduleJob | None:
        """Update only the claimed job, preserving concurrent add/remove edits."""
        with self._locked():
            jobs = list(self._load_jobs_unlocked())
            for index, job in enumerate(jobs):
                if job.job_id == job_id:
                    updated = job.model_copy(
                        update={"last_status": status, "last_error": error}
                    )
                    jobs[index] = updated
                    self._save_jobs_unlocked(tuple(jobs))
                    return updated
            return None

    def load_runs(self, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Newest-first run history for display; malformed lines are skipped."""
        if limit < 1:
            return ()
        try:
            lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        except OSError:
            raise ValueError("schedule run history is unreadable") from None
        rows: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
            if len(rows) >= limit:
                break
        return tuple(rows)

    def _append_run_unlocked(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        with self.runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            self.runs_path.chmod(0o600)

    def append_run(self, record: dict[str, Any]) -> None:
        with self._locked():
            self._append_run_unlocked(record)
