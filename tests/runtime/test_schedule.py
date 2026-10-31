"""Offline scheduler regressions: fake clock, persistence, delivery, no replay."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cairn.core.schedule import (
    ScheduleJob,
    ScheduleKind,
    initial_next_run,
)
from cairn.infra.schedule.store import ScheduleStore
from cairn.runtime.scheduling.runner import ScheduleRunner
from cairn.terminal.headless import HeadlessResult, HeadlessStatus

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _job(**overrides: object) -> ScheduleJob:
    values: dict[str, object] = {
        "job_id": "job-1",
        "name": "daily summary",
        "prompt": "summarize",
        "recipient": "owner",
        "kind": ScheduleKind.ONCE,
        "at": NOW,
        "created_at": NOW,
        "next_run_at": NOW,
    }
    values.update(overrides)
    return ScheduleJob.model_validate(values)


def _result(task_id: str = "scheduled-job-1") -> HeadlessResult:
    return HeadlessResult(
        task_id=task_id,
        status=HeadlessStatus.COMPLETED,
        final_response="done",
        verified=True,
        workspace="/tmp/ws",
    )


def test_initial_next_run_daily_and_interval() -> None:
    daily = initial_next_run(
        ScheduleKind.DAILY, now=NOW, daily_time="10:00", timezone="Asia/Shanghai"
    )
    assert daily.hour == 10 and daily.date() == NOW.date()
    tomorrow = initial_next_run(
        ScheduleKind.DAILY, now=NOW, daily_time="08:00", timezone="Asia/Shanghai"
    )
    assert tomorrow.date() == (NOW + timedelta(days=1)).date()
    interval = initial_next_run(ScheduleKind.INTERVAL, now=NOW, interval_seconds=3600)
    assert interval == NOW + timedelta(hours=1)


def test_runner_runs_due_once_and_does_not_replay_after_restart(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    calls: list[tuple[str, str]] = []
    sent: list[tuple[str, str]] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append((prompt, task_id))
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        sent.append((sender, text))
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)
        assert calls == [("summarize", "scheduled-job-1")]
        assert sent and sent[0][0] == "owner" and "done" in sent[0][1]
        # Reload to model a restart: the advanced next_run is durable.
        runner2 = ScheduleRunner(
            store=ScheduleStore(tmp_path / "schedule"),
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner2.run_due(NOW)
        assert calls == [("summarize", "scheduled-job-1")]

    asyncio.run(scenario())
    saved = store.load_jobs()
    assert saved[0].next_run_at is None
    assert saved[0].last_status == "completed"


def test_runner_marks_delivery_failure_without_claiming_success(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return _result(task_id)

    async def no_delivery(sender: str, text: str) -> bool:
        return False

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=no_delivery,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)
        assert runner.report.delivery_failed == 1

    asyncio.run(scenario())


def test_interval_job_advances_and_remains_due_later(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    job = _job(
        kind=ScheduleKind.INTERVAL,
        at=None,
        interval_seconds=60,
        next_run_at=NOW,
    )
    store.save_jobs((job,))
    count = 0

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal count
        count += 1
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)
        assert count == 1
        assert store.load_jobs()[0].next_run_at == NOW + timedelta(seconds=60)
        await runner.run_due(NOW + timedelta(seconds=60))
        assert count == 2

    asyncio.run(scenario())


def test_job_added_during_execution_is_not_overwritten(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        started.set()
        await release.wait()
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        running = asyncio.create_task(runner.run_due(NOW))
        await started.wait()
        added = _job(
            job_id="job-2",
            next_run_at=NOW + timedelta(hours=1),
        )
        store.add_job(added)
        release.set()
        await running
        ids = [job.job_id for job in store.load_jobs()]
        assert ids == ["job-1", "job-2"]
        by_id = {job.job_id: job for job in store.load_jobs()}
        assert by_id["job-1"].last_status == "completed"
        assert by_id["job-2"].last_status is None

    asyncio.run(scenario())


def test_two_sessions_do_not_claim_same_due_job(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def first_executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _result(task_id)

    async def second_executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal calls
        calls += 1
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        first = ScheduleRunner(
            store=store,
            executor=first_executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        second = ScheduleRunner(
            store=ScheduleStore(tmp_path / "schedule"),
            executor=second_executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        running = asyncio.create_task(first.run_due(NOW))
        await started.wait()
        waiting = asyncio.create_task(second.run_due(NOW))
        await asyncio.sleep(0.05)
        assert calls == 1
        release.set()
        await asyncio.gather(running, waiting)
        assert calls == 1

    asyncio.run(scenario())


def test_claimed_unfinished_job_is_not_replayed_after_restart(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    claimed = store.claim_due(NOW)
    assert claimed is not None
    calls = 0

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal calls
        calls += 1
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=ScheduleStore(tmp_path / "schedule"),
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)
        await runner.run_due(NOW)
        assert calls == 0

    asyncio.run(scenario())
    saved = store.load_jobs()[0]
    assert saved.last_status == "interrupted"
    assert saved.next_run_at is None
    rows = store.load_runs()
    assert [row["status"] for row in rows] == ["interrupted", "started"]
    assert rows[0]["run_key"] == rows[1]["run_key"]
    assert rows[0]["delivered"] is None


def test_recovery_does_not_mark_another_owner_interrupted(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs(
        (_job(workspace="/workspace-a", recipient="alice", account_id="bot-a"),)
    )
    assert store.claim_due(
        NOW, workspace="/workspace-a", recipient="alice", account_id="bot-a"
    )

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        raise AssertionError("the abandoned occurrence must not be replayed")

    async def deliver(sender: str, text: str) -> bool:
        raise AssertionError("an unknown result must not be delivered")

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "bob",
            workspace="/workspace-b",
            account_id="bot-b",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)

    asyncio.run(scenario())
    assert store.load_jobs()[0].last_status == "running"
    assert [row["status"] for row in store.load_runs()] == ["started"]


def test_delivery_is_not_redirected_when_account_or_recipient_changes(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(recipient="alice", account_id="bot-B"),))
    sent: list[tuple[str, str, str]] = []
    state = {"account": "bot-B", "recipient": "alice"}

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        # Simulate a login/switch while the task is running.
        state["account"] = "bot-A"
        state["recipient"] = "bob"
        return _result(task_id)

    async def deliver_for_account(sender: str, text: str, account: str) -> bool:
        sent.append((sender, text, account))
        return True

    async def deliver(sender: str, text: str) -> bool:
        raise AssertionError("redirected delivery must not run")

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            deliver_for_account=deliver_for_account,
            recipient=lambda: state["recipient"],
            account_id=lambda: state["account"],
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)

    asyncio.run(scenario())
    assert sent == []
    saved = store.load_jobs()[0]
    assert saved.last_status == "completed"
    runs = store.load_runs()
    assert "delivery_owner_changed" in str(runs[0].get("error"))
    assert runs[0]["task_id"] == "scheduled-job-1"


def test_failed_delivery_keeps_bounded_result_in_history(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(recipient="owner"),))

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            final_response="UNIQUE-SCHEDULE-RESULT",
            workspace=str(tmp_path),
        )

    async def deliver(sender: str, text: str) -> bool:
        return False

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)

    asyncio.run(scenario())
    run = store.load_runs()[0]
    assert run["delivered"] is False
    assert "UNIQUE-SCHEDULE-RESULT" in run["result"]


def test_delivery_cancellation_reports_unconfirmed_and_keeps_task_fact(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(recipient="owner"),))
    emits: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            final_response="task result",
            workspace=str(tmp_path),
        )

    async def deliver(sender: str, text: str) -> bool:
        raise asyncio.CancelledError()

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            emit=emits.append,
            clock=lambda: NOW,
        )
        with pytest.raises(asyncio.CancelledError):
            await runner.run_due(NOW)

    asyncio.run(scenario())
    run = store.load_runs()[0]
    assert run["status"] == "completed"
    assert run["task_id"] == "scheduled-job-1"
    assert "delivery_cancelled" in str(run["error"])
    assert any("投递未确认" in line for line in emits)


def test_controller_close_cancels_waiting_for_execution_lock(tmp_path: Path) -> None:
    import time

    from cairn.terminal.schedule.controller import ScheduleController

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    holder = store.try_execution_lock()
    assert holder is not None

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        raise AssertionError("must not execute while lock is held")

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        controller = ScheduleController(
            workspace=workspace,
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            account_id=lambda: "bot-test",
            clock=lambda: NOW,
        )
        await controller.start()
        await asyncio.sleep(0.1)
        started = time.monotonic()
        await controller.close()
        assert time.monotonic() - started < 0.5

    try:
        asyncio.run(scenario())
    finally:
        holder.release()


def test_controller_close_marks_running_job_cancelled_and_no_replay(
    tmp_path: Path,
) -> None:
    import time

    from cairn.terminal.schedule.controller import ScheduleController

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(workspace=str(workspace), account_id="bot-test"),))
    started = asyncio.Event()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        started.set()
        await asyncio.sleep(10)
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        controller = ScheduleController(
            workspace=workspace,
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            account_id=lambda: "bot-test",
            clock=lambda: NOW,
        )
        await controller.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        began = time.monotonic()
        await controller.close()
        assert time.monotonic() - began < 0.5

    asyncio.run(scenario())
    saved = store.load_jobs()[0]
    assert saved.last_status == "cancelled"
    runs = store.load_runs()
    assert runs[0]["status"] == "cancelled"
    # A restart must not replay the cancelled side effect.
    calls = 0

    async def late_executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal calls
        calls += 1
        return _result(task_id)

    async def late_run() -> None:
        runner = ScheduleRunner(
            store=ScheduleStore(tmp_path / "schedule"),
            executor=late_executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)

    asyncio.run(late_run())
    assert calls == 0


def test_controller_close_propagates_late_cancellation_after_cleanup(
    tmp_path: Path,
) -> None:
    import time

    from cairn.terminal.schedule.controller import ScheduleController

    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(workspace=str(workspace), account_id="bot-test"),))
    started = asyncio.Event()
    cleaning = asyncio.Event()

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cleaning.set()
            await asyncio.sleep(0.2)
            raise
        raise AssertionError("unreachable")

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        controller = ScheduleController(
            workspace=workspace,
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            account_id=lambda: "bot-test",
            clock=lambda: NOW,
        )
        await controller.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        closing = asyncio.create_task(controller.close())
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        began = time.monotonic()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert time.monotonic() - began < 1.0
        assert controller._task is None

    asyncio.run(scenario())


def test_waiting_for_execution_lock_cancels_without_waiting_for_holder(
    tmp_path: Path,
) -> None:
    import time

    import pytest

    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    holder = store.try_execution_lock()
    assert holder is not None
    called = False

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal called
        called = True
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        waiting = asyncio.create_task(runner.run_due(NOW))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert time.monotonic() - started < 0.5
        assert called is False

    try:
        asyncio.run(scenario())
    finally:
        holder.release()


def test_waiting_for_lock_rechecks_identity_before_claim(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(recipient="alice", account_id="bot-A"),))
    holder = store.try_execution_lock()
    assert holder is not None
    state = {"account": "bot-A", "recipient": "alice"}
    calls = 0

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal calls
        calls += 1
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: state["recipient"],
            account_id=lambda: state["account"],
            clock=lambda: NOW,
        )
        waiting = asyncio.create_task(runner.run_due(NOW))
        await asyncio.sleep(0.1)
        state.update(account="bot-B", recipient="bob")
        holder.release()
        await waiting

    asyncio.run(scenario())
    assert calls == 0


def test_executor_error_redacts_known_secrets_from_job_and_history(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    secret = "FAKE-SCHEDULE-EXCEPTION-SECRET"
    messages: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        raise RuntimeError(f"unexpected credential {secret}")

    async def deliver(sender: str, text: str) -> bool:
        raise AssertionError("a failed execution must not deliver a result")

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
            emit=messages.append,
            result_redactor=lambda text: text.replace(secret, "[REDACTED]"),
        )
        await runner.run_due(NOW)

    asyncio.run(scenario())
    assert store.load_jobs()[0].last_status == "failed"
    assert "[REDACTED]" in str(store.load_runs()[0]["error"])
    assert secret not in store.jobs_path.read_text(encoding="utf-8")
    assert secret not in store.runs_path.read_text(encoding="utf-8")
    assert all(secret not in message for message in messages)


@pytest.mark.parametrize("terminal_status", ["completed", "interrupted"])
def test_recovery_keeps_a_persisted_terminal_fact_after_job_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    claim = store.claim_due(NOW)
    assert claim is not None
    _, run_key = claim
    original_save = store._save_jobs_unlocked

    def fail_save(jobs: tuple[ScheduleJob, ...]) -> None:
        raise OSError("simulated crash before job-status write")

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        raise AssertionError("a claimed occurrence must not be replayed")

    async def deliver(sender: str, text: str) -> bool:
        raise AssertionError("recovery must not deliver again")

    async def recover() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)

    monkeypatch.setattr(store, "_save_jobs_unlocked", fail_save)
    if terminal_status == "completed":
        with pytest.raises(OSError):
            store.finish_run(
                "job-1",
                status="completed",
                error=None,
                record={
                    "run_key": run_key,
                    "job_id": "job-1",
                    "finished_at": NOW.isoformat(),
                    "status": "completed",
                    "delivered": True,
                    "error": None,
                },
            )
    else:
        with pytest.raises(OSError):
            asyncio.run(recover())
    assert store.load_jobs()[0].last_status == "running"
    monkeypatch.setattr(store, "_save_jobs_unlocked", original_save)
    asyncio.run(recover())
    asyncio.run(recover())

    assert store.load_jobs()[0].last_status == terminal_status
    rows = store.load_runs()
    assert [row["status"] for row in rows] == [terminal_status, "started"]
    assert rows[0]["run_key"] == run_key
    assert rows[0]["delivered"] is (True if terminal_status == "completed" else None)


def test_runner_supports_async_emit_callback(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule")
    store.save_jobs((_job(),))
    emitted: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return _result(task_id)

    async def deliver(sender: str, text: str) -> bool:
        return True

    async def emit(text: str) -> None:
        emitted.append(text)

    async def scenario() -> None:
        runner = ScheduleRunner(
            store=store,
            executor=executor,
            deliver=deliver,
            recipient=lambda: "owner",
            emit=emit,
            clock=lambda: NOW,
        )
        await runner.run_due(NOW)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert any("定时任务 job-1" in text for text in emitted)
