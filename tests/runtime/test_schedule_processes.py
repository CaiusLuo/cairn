"""Cross-process scheduler single-task regression (Linux/Debian)."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cairn.core.schedule import ScheduleJob, ScheduleKind
from cairn.infra.schedule.store import ScheduleStore
from cairn.runtime.scheduling.runner import ScheduleRunner
from cairn.terminal.headless import HeadlessResult, HeadlessStatus

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _job(job_id: str) -> ScheduleJob:
    return ScheduleJob(
        job_id=job_id,
        name=job_id,
        prompt=job_id,
        recipient="owner",
        kind=ScheduleKind.ONCE,
        at=NOW,
        created_at=NOW,
        next_run_at=NOW,
    )


def _worker(
    store_root: str,
    name: str,
    log_path: str,
    release: multiprocessing.synchronize.Event,
) -> None:
    def record(value: str) -> None:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        record(name)
        if name == "first":
            await asyncio.to_thread(release.wait)
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=store_root,
        )

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def run() -> None:
        runner = ScheduleRunner(
            store=ScheduleStore(Path(store_root)),
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW, max_jobs=1)

    asyncio.run(run())


def _lines(path: Path) -> list[str]:
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    except FileNotFoundError:
        return []


def test_two_processes_execute_different_jobs_serially(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    root = str(tmp_path / "schedule")
    log = tmp_path / "events.txt"
    store = ScheduleStore(Path(root))
    store.save_jobs((_job("first"), _job("second")))
    release = ctx.Event()
    first = ctx.Process(target=_worker, args=(root, "first", str(log), release))
    second = ctx.Process(target=_worker, args=(root, "second", str(log), release))
    first.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _lines(log) != ["first"]:
        time.sleep(0.02)
    assert _lines(log) == ["first"]
    second.start()
    time.sleep(0.3)
    # Different due job, but the second process must wait for the global lock.
    assert _lines(log) == ["first"]
    assert store.load_jobs()[0].last_status == "running"
    assert all(row["status"] != "interrupted" for row in store.load_runs())
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert first.exitcode == 0 and second.exitcode == 0
    assert _lines(log) == ["first", "second"]


def test_killed_executor_is_reported_interrupted_without_replay(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    root = str(tmp_path / "schedule")
    log = tmp_path / "events.txt"
    store = ScheduleStore(Path(root))
    store.save_jobs((_job("first"),))
    release = ctx.Event()
    first = ctx.Process(target=_worker, args=(root, "first", str(log), release))
    first.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _lines(log) != ["first"]:
            time.sleep(0.02)
        assert _lines(log) == ["first"]
        first.terminate()
        first.join(timeout=10)
        assert first.exitcode is not None

        async def executor(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            raise AssertionError("an interrupted occurrence must not be replayed")

        async def deliver(sender: str, text: str) -> bool:
            raise AssertionError("delivery status after a crash is unknown")

        async def recover() -> None:
            runner = ScheduleRunner(
                store=ScheduleStore(Path(root)),
                executor=executor,
                deliver=deliver,
                recipient=lambda: "owner",
                clock=lambda: NOW,
            )
            await runner.run_due(NOW)
            await runner.run_due(NOW)

        asyncio.run(recover())
        assert store.load_jobs()[0].last_status == "interrupted"
        runs = store.load_runs()
        assert [row["status"] for row in runs] == ["interrupted", "started"]
        assert runs[0]["run_key"] == runs[1]["run_key"]
        assert runs[0]["delivered"] is None
        assert _lines(log) == ["first"]
    finally:
        if first.is_alive():
            first.terminate()
            first.join(timeout=10)
