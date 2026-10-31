"""Bounded scheduler loop for host-authored tasks.

One owner, one due decision at a time. Facts are persisted before execution and
again after it, so a restart cannot silently replay a started job. Delivery is an
injected effect: the runtime knows nothing about WeChat or the terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from cairn.core.schedule import ScheduleJob
from cairn.infra.schedule.store import ScheduleStore


class TaskOutcome(Protocol):
    task_id: str
    status: Any
    final_response: str | None
    verified: bool
    error_category: str | None
    trace_id: str | None


TaskExecutor = Callable[[str, asyncio.Event, str], Awaitable[TaskOutcome]]
Deliver = Callable[[str, str], Awaitable[bool]]
DeliverForAccount = Callable[[str, str, str], Awaitable[bool]]
Recipient = Callable[[], str | None]
Clock = Callable[[], datetime]


def current_time() -> datetime:
    return datetime.now().astimezone()


@dataclass(slots=True)
class ScheduleReport:
    ticks: int = 0
    jobs_run: int = 0
    delivered: int = 0
    delivery_failed: int = 0
    failed: int = 0
    skipped: int = 0


def format_schedule_result(result: TaskOutcome) -> str:
    lines = [f"定时任务 {result.task_id}", f"状态: {result.status.value}"]
    if result.verified:
        lines.append("宿主检查: 通过")
    if result.final_response:
        body = result.final_response.strip()
        lines.append("")
        lines.append(body[:3000] + ("..." if len(body) > 3000 else ""))
    if result.error_category:
        lines.append(f"错误类别: {result.error_category}")
    if result.trace_id:
        lines.append(f"trace: {result.trace_id}")
    return "\n".join(lines)[:4000]


@asynccontextmanager
async def _execution_guard(store: ScheduleStore) -> AsyncIterator[None]:
    lease = None
    while lease is None:
        lease = store.try_execution_lock()
        if lease is None:
            # Sleep in the event loop, not a blocking flock thread, so a caller
            # cancellation is observed immediately during process shutdown.
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lease.release()


class ScheduleRunner:
    def __init__(
        self,
        *,
        store: ScheduleStore,
        executor: TaskExecutor,
        deliver: Deliver,
        recipient: Recipient,
        deliver_for_account: DeliverForAccount | None = None,
        clock: Clock = current_time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval: float = 5.0,
        emit: Callable[[str], object] | None = None,
        workspace: str = "",
        account_id: str | Callable[[], str | None] | None = None,
        result_redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.deliver = deliver
        self.deliver_for_account = deliver_for_account
        self.recipient = recipient
        self.workspace = workspace
        self.account_id = account_id
        self.result_redactor = result_redactor
        self.clock = clock
        self.sleep = sleep
        self.poll_interval = max(0.05, poll_interval)
        self.emit = emit
        self.report = ScheduleReport()

    async def run(self, stop: asyncio.Event | None = None) -> ScheduleReport:
        waiter = stop if stop is not None else asyncio.Event()
        while not waiter.is_set():
            self.report.ticks += 1
            try:
                await self.run_due(self.clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._say(f"定时任务检查失败: {type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter.wait(), timeout=self.poll_interval)
        return self.report

    async def run_due(
        self, now: datetime, *, max_jobs: int | None = None
    ) -> ScheduleReport:
        # Hold the cross-process execution lock across claim + executor. A
        # different due job in another session must wait, not run concurrently.
        async with _execution_guard(self.store):
            executed = 0
            while max_jobs is None or executed < max_jobs:
                # Re-read identity before every claim: waiting for the lock or
                # completing a previous job may have changed account/recipient.
                owner = self.recipient()
                account = (
                    self.account_id() if callable(self.account_id) else self.account_id
                )
                if not owner or (self.account_id is not None and not account):
                    return self.report
                self.store.recover_interrupted(
                    self.clock(),
                    workspace=self.workspace or None,
                    recipient=owner,
                    account_id=account if self.account_id is not None else None,
                )
                claim = self.store.claim_due(
                    now,
                    workspace=self.workspace or None,
                    recipient=owner,
                    account_id=account if self.account_id is not None else None,
                )
                if claim is None:
                    return self.report
                job, run_key = claim
                await self._run_job(job, now, run_key, owner, account)
                executed += 1
            return self.report

    def _record_finish(
        self,
        *,
        job: ScheduleJob,
        run_key: str,
        status: str,
        error: str | None,
        result: TaskOutcome | None,
        delivered: bool,
    ) -> None:
        result_text = (
            format_schedule_result(result)[:4000] if result is not None else ""
        )
        if self.result_redactor is not None:
            result_text = self.result_redactor(result_text)
            if error is not None:
                error = self.result_redactor(error)
        self.store.finish_run(
            job.job_id,
            status=status,
            error=error,
            record={
                "run_key": run_key,
                "job_id": job.job_id,
                "workspace": job.workspace,
                "recipient": job.recipient,
                "account_id": job.account_id,
                "finished_at": self.clock().isoformat(),
                "status": status,
                "task_id": result.task_id if result is not None else None,
                "trace_id": result.trace_id if result is not None else None,
                "delivered": delivered,
                "error": error,
                "result": result_text,
            },
        )
        if delivered:
            self.report.delivered += 1
        else:
            self.report.delivery_failed += 1
        if status not in {"completed", "cancelled"}:
            self.report.failed += 1
        self.report.jobs_run += 1
        if error is not None and (
            error.startswith("delivery_cancelled")
            or error.startswith("delivery_owner_changed")
        ):
            delivery_label = "投递未确认"
        else:
            delivery_label = "已推送" if delivered else "未推送"
        self._say(f"定时任务 {job.job_id}: {status}; {delivery_label}")

    async def _run_job(
        self,
        job: ScheduleJob,
        now: datetime,
        run_key: str,
        owner: str,
        account: str | None,
    ) -> None:
        result: TaskOutcome | None = None
        status = "failed"
        error: str | None = None
        try:
            result = await self.executor(
                job.prompt, asyncio.Event(), f"scheduled-{job.job_id}"
            )
            status = result.status.value
        except asyncio.CancelledError:
            self._record_finish(
                job=job,
                run_key=run_key,
                status="cancelled",
                error="cancelled",
                result=None,
                delivered=False,
            )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        delivered = False
        if result is not None:
            current_account = (
                self.account_id() if callable(self.account_id) else self.account_id
            )
            if self.account_id is not None and current_account != account:
                # Never send a completed result through a newly selected identity.
                error = "delivery_owner_changed: execution result stored, delivery not attempted"
            else:
                recipient = self.recipient()
                if recipient != owner:
                    # Never redirect a completed result to a newly selected chat.
                    error = "delivery_owner_changed: result retained, not delivered"
                elif recipient:
                    try:
                        payload = format_schedule_result(result)
                        if self.result_redactor is not None:
                            payload = self.result_redactor(payload)
                        if self.deliver_for_account is not None and account is not None:
                            delivered = await self.deliver_for_account(
                                recipient, payload, account
                            )
                        else:
                            delivered = await self.deliver(recipient, payload)
                    except asyncio.CancelledError:
                        # The task result is known and must be preserved, but the push
                        # is not confirmed. Never claim delivery or replay the task.
                        self._record_finish(
                            job=job,
                            run_key=run_key,
                            status=status,
                            error="delivery_cancelled: result not confirmed delivered",
                            result=result,
                            delivered=False,
                        )
                        raise
                    except Exception as exc:
                        error = f"delivery: {type(exc).__name__}: {exc}"
        else:
            error = error or "task produced no result"
        self._record_finish(
            job=job,
            run_key=run_key,
            status=status,
            error=error,
            result=result,
            delivered=delivered,
        )

    def _say(self, text: str) -> None:
        if self.emit is None:
            return
        result = self.emit(self.result_redactor(text) if self.result_redactor else text)
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            task.add_done_callback(self._consume_emit)

    @staticmethod
    def _consume_emit(task: asyncio.Future[Any]) -> None:
        """Retrieve an async UI failure so it is not reported as unhandled."""
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
