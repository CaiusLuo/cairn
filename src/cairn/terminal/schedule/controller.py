"""Terminal-facing lifecycle and commands for scheduled tasks."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path

from cairn.core.schedule import (
    ScheduleJob,
    ScheduleKind,
    initial_next_run,
)
from cairn.infra.schedule.store import ScheduleStore, ensure_outside_workspace
from cairn.runtime.lifecycle import await_owned
from cairn.runtime.scheduling.runner import ScheduleRunner, current_time
from cairn.terminal.action import Action, correction, note
from cairn.terminal.headless import HeadlessResult

TaskExecutor = Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]
Deliver = Callable[[str, str], Awaitable[bool]]
Recipient = Callable[[], str | None]


class ScheduleController:
    def __init__(
        self,
        *,
        workspace: Path,
        store: ScheduleStore,
        executor: TaskExecutor,
        deliver: Deliver,
        recipient: Recipient,
        deliver_for_account: Callable[[str, str, str], Awaitable[bool]] | None = None,
        emit: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] = current_time,
        account_id: Callable[[], str | None] | None = None,
        result_redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.workspace_key = str(workspace.resolve())
        self.recipient = recipient
        self.account_id = account_id or (lambda: None)
        self.result_redactor = result_redactor
        self.store = store
        self.deliver = deliver
        self.runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            deliver_for_account=deliver_for_account,
            recipient=recipient,
            clock=clock,
            emit=emit,
            workspace=self.workspace_key,
            account_id=self.account_id,
            result_redactor=self.result_redactor,
        )
        self.clock = clock
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[object] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        ensure_outside_workspace(self.store.root, self.workspace)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.runner.run(self._stop))
        # Let an already-due job claim the shared task gate before the first
        # local turn starts; otherwise it would be canceled by immediate /exit.
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self._task is None or self._task.done():
            self._task = None
            return
        assert self._stop is not None
        self._stop.set()
        # A runner waiting for the cross-process execution lock only observes
        # cancellation, not the stop event; cancel it so CLI exit does not wait
        # for another process's long task.
        self._task.cancel()
        late: asyncio.CancelledError | None = None
        try:
            late = await await_owned(self._task)
        finally:
            self._task = None
        if late is not None:
            # Cleanup completed; a cancellation that arrived while waiting for it
            # must still reach the caller.
            raise late

    async def command(self, args: str) -> Action:
        parts = args.split()
        if not parts or parts[0] in {"help"}:
            return note(
                "/schedule add once <ISO> <prompt> | add daily <HH:MM> <prompt> | "
                "add interval <seconds> <prompt> | list | history [<id>] | "
                "remove <id> | pause/resume <id>"
            )
        verb = parts[0].lower()
        if verb == "list":
            return self._list()
        if verb in {"history", "runs"}:
            return self._history(parts[1:])
        if verb in {"remove", "pause", "resume"}:
            return self._mutate(verb, parts[1:])
        if verb == "add":
            return self._add(parts[1:])
        return correction("未知 /schedule 子命令; 使用 /schedule help. ")

    def _owned(self, job: ScheduleJob) -> bool:
        owner = self.recipient()
        account = self.account_id()
        if not owner or not account:
            return False
        return (
            job.recipient == owner
            and job.workspace == self.workspace_key
            and job.account_id == account
        )

    def _list(self) -> Action:
        try:
            jobs = self.store.load_jobs()
        except ValueError as exc:
            return correction(f"定时任务存储不可用: {exc}")
        if not jobs:
            return note("没有定时任务. ")
        lines = []
        owned = [job for job in jobs if self._owned(job)]
        other = len(jobs) - len(owned)
        for job in owned:
            state = "启用" if job.enabled else "暂停"
            next_run = job.next_run_at.isoformat() if job.next_run_at else "已完成"
            last = job.last_status or "never"
            at = job.last_run_at.isoformat() if job.last_run_at else "-"
            lines.append(
                f"{job.job_id} [{state}] next={next_run} last={last} at={at} "
                f"{job.name or job.prompt[:40]}"
            )
        if other:
            lines.append(
                f"另有 {other} 个属于其他 Workspace/账号或未归属的任务, 不可操作. "
            )
        if not owned and other:
            return note("没有当前 Workspace 的定时任务. \n" + "\n".join(lines))
        return note("\n".join(lines))

    def _history(self, args: Sequence[str]) -> Action:
        try:
            rows = self.store.load_runs(limit=50)
        except (OSError, ValueError) as exc:
            return correction(f"读取运行历史失败: {exc}")
        owner = self.recipient()
        account = self.account_id()
        if not owner or not account:
            return note("微信账号尚未确定, 无法查询运行历史。")
        rows = tuple(
            row
            for row in rows
            if row.get("workspace") == self.workspace_key
            and row.get("recipient") == owner
            and row.get("account_id") == account
        )
        if args:
            rows = tuple(row for row in rows if row.get("job_id") == args[0])
        if not rows:
            return note("没有运行历史. ")
        lines = []
        for row in rows[:12]:
            lines.append(
                f"{row.get('job_id')} {row.get('status')} "
                f"at={row.get('finished_at') or row.get('started_at')} "
                f"delivered={row.get('delivered', '-')} "
                f"task={row.get('task_id') or '-'} trace={row.get('trace_id') or '-'}"
                + (f" error={row.get('error')}" if row.get("error") else "")
            )
            if row.get("result"):
                lines.append(f"  result: {str(row.get('result'))[:500]}")
        return note("\n".join(lines))

    def _mutate(self, verb: str, args: Sequence[str]) -> Action:
        if len(args) != 1:
            return correction(f"用法: /schedule {verb} <id>")
        job_id = args[0]
        try:
            job = next(
                (item for item in self.store.load_jobs() if item.job_id == job_id), None
            )
        except (OSError, ValueError) as exc:
            return correction(f"更新定时任务失败: {exc}")
        if job is None:
            return correction(f"找不到任务: {job_id}")
        if not self._owned(job):
            return correction("该任务属于其他 Workspace/账号或缺少归属, 不能操作. ")
        try:
            if verb == "remove":
                changed = self.store.remove_job(job_id)
            else:
                changed = self.store.set_enabled(job_id, verb == "resume")
        except (OSError, ValueError) as exc:
            return correction(f"更新定时任务失败: {exc}")
        if not changed:
            return correction(f"找不到任务: {job_id}")
        label = {"remove": "删除", "pause": "暂停", "resume": "恢复"}[verb]
        return note(f"已{label}任务 {job_id}. ")

    def _add(self, args: Sequence[str]) -> Action:
        if len(args) < 3:
            return correction(
                "用法: /schedule add once|daily|interval <时间/秒> <prompt>"
            )
        kind_text, when = args[0].lower(), args[1]
        prompt = " ".join(args[2:]).strip()
        if not prompt:
            return correction("定时任务 prompt 不能为空. ")
        owner = self.recipient()
        account = self.account_id()
        if owner is None or account is None:
            return correction("微信账号尚未确定; 请先完成 /wx 登录再创建任务。")
        try:
            kind = ScheduleKind(kind_text)
            now = self.clock()
            at = None
            daily_time = None
            interval_seconds = None
            if kind is ScheduleKind.ONCE:
                at = datetime.fromisoformat(when)
            elif kind is ScheduleKind.DAILY:
                daily_time = when
            else:
                interval_seconds = int(when)
            next_run = initial_next_run(
                kind,
                now=now,
                at=at,
                daily_time=daily_time,
                interval_seconds=interval_seconds,
            )
            job = ScheduleJob(
                job_id="job-" + uuid.uuid4().hex[:12],
                name=prompt[:60],
                prompt=prompt,
                workspace=self.workspace_key,
                recipient=owner,
                account_id=account,
                kind=kind,
                at=at,
                daily_time=daily_time,
                interval_seconds=interval_seconds,
                created_at=now,
                next_run_at=next_run,
            )
        except (ValueError, TypeError) as exc:
            return correction(f"定时任务参数无效: {exc}")
        try:
            self.store.add_job(job)
        except (OSError, ValueError) as exc:
            return correction(f"保存定时任务失败: {exc}")
        return note(f"已创建定时任务 {job.job_id}, 下次运行: {next_run.isoformat()}")
