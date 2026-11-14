"""The host review schedule is cut-off based, restart-safe and idempotent."""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from cairn.review.models import ReviewStatus
from cairn.review.schedule import ReviewScheduleCoordinator
from cairn.scheduling.models import ScheduleJob, ScheduleKind
from cairn.scheduling.store import ScheduleStore

TZ = "Asia/Shanghai"
AFTER_CUTOFF = datetime(2026, 1, 15, 20, 31, tzinfo=ZoneInfo(TZ))
BEFORE_CUTOFF = datetime(2026, 1, 15, 20, 29, tzinfo=ZoneInfo(TZ))
NEXT_MORNING = datetime(2026, 1, 16, 9, 0, tzinfo=ZoneInfo(TZ))


class FakeService:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.settled: list[Any] = []
        self._counter = 0

    async def generate_daily_review(
        self,
        day: Any,
        request_id: str,
        *,
        timezone: str = "Asia/Shanghai",
        regenerate: bool = False,
        cutoff: Any = None,
        deliver: bool = False,
    ) -> Any:
        if self.fail is not None:
            raise self.fail
        self.calls.append(
            {
                "day": day,
                "request_id": request_id,
                "timezone": timezone,
                "regenerate": regenerate,
                "cutoff": cutoff,
                "deliver": deliver,
            }
        )
        self._counter += 1
        return SimpleNamespace(
            review_id=f"review-{self._counter}",
            status=ReviewStatus.PENDING,
            day=day,
        )

    async def wait_for_review(self, review_id: str, *, timeout: float) -> Any:
        return SimpleNamespace(
            review_id=review_id,
            status=ReviewStatus.COMPLETED,
        )


class GatedService(FakeService):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def generate_daily_review(
        self,
        day: Any,
        request_id: str,
        *,
        timezone: str = "Asia/Shanghai",
        regenerate: bool = False,
        cutoff: Any = None,
        deliver: bool = False,
    ) -> Any:
        self.started.set()
        await self.gate.wait()
        return await super().generate_daily_review(
            day,
            request_id,
            timezone=timezone,
            regenerate=regenerate,
            cutoff=cutoff,
            deliver=deliver,
        )


def _coordinator(
    tmp_path: Path,
    service: FakeService,
    *,
    clock: Any = AFTER_CUTOFF,
    on_settled: Any = None,
) -> tuple[ReviewScheduleCoordinator, ScheduleStore]:
    store = ScheduleStore(tmp_path / "schedules")
    coordinator = ReviewScheduleCoordinator(
        service=service,  # type: ignore[arg-type]
        store=store,
        on_settled=on_settled,
        clock=lambda: clock,
        settle_timeout=1.0,
    )
    return coordinator, store


def test_cutoff_day_is_the_most_recent_2030_boundary(tmp_path: Path) -> None:
    service = FakeService()
    coordinator, _ = _coordinator(tmp_path, service)
    for moment, expected in (
        (AFTER_CUTOFF, "2026-01-15"),
        (BEFORE_CUTOFF, "2026-01-14"),
        (NEXT_MORNING, "2026-01-15"),
    ):
        due = coordinator.due_day(moment)
        assert due is not None
        assert due.isoformat() == expected


def test_tick_generates_once_with_cutoff_and_delivery_intent(
    tmp_path: Path,
) -> None:
    service = FakeService()
    settled: list[Any] = []
    coordinator, _ = _coordinator(tmp_path, service, on_settled=settled.append)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert service.calls == [
            {
                "day": AFTER_CUTOFF.date(),
                "request_id": "host-daily-review:2026-01-15:Asia/Shanghai",
                "timezone": TZ,
                "regenerate": False,
                "cutoff": time(20, 30),
                "deliver": True,
            }
        ]
        assert [row.status for row in settled] == [ReviewStatus.COMPLETED]

        # A later tick on the same cutoff day must not duplicate the report.
        assert await coordinator.tick() is None
        assert len(service.calls) == 1

    asyncio.run(scenario())


def test_restart_uses_the_persisted_attempt_and_does_not_duplicate(
    tmp_path: Path,
) -> None:
    service = FakeService()
    coordinator, store = _coordinator(tmp_path, service)
    asyncio.run(coordinator.tick())
    assert len(service.calls) == 1

    restarted_service = FakeService()
    restarted = ReviewScheduleCoordinator(
        service=restarted_service,  # type: ignore[arg-type]
        store=store,
        clock=lambda: AFTER_CUTOFF,
    )
    asyncio.run(restarted.tick())
    assert restarted_service.calls == []


def test_missed_cutoff_is_recovered_on_the_next_start(tmp_path: Path) -> None:
    service = FakeService()
    coordinator, _ = _coordinator(tmp_path, service, clock=NEXT_MORNING)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert service.calls[0]["day"].isoformat() == "2026-01-15"

    asyncio.run(scenario())


def test_ordinary_schedule_jobs_are_not_created_or_modified(
    tmp_path: Path,
) -> None:
    service = FakeService()
    coordinator, store = _coordinator(tmp_path, service)
    job = ScheduleJob(
        job_id="ordinary",
        prompt="ordinary task",
        kind=ScheduleKind.DAILY,
        daily_time="09:00",
        workspace="/workspace/cairn",
        recipient="owner",
        account_id="bot",
        created_at=AFTER_CUTOFF,
        next_run_at=datetime(2026, 1, 16, 9, 0, tzinfo=ZoneInfo(TZ)),
    )
    store.add_job(job)

    asyncio.run(coordinator.tick())
    jobs = store.load_jobs()
    assert [item.job_id for item in jobs] == ["ordinary"]
    assert jobs[0].prompt == "ordinary task"


def test_concurrent_ticks_share_the_lock_and_generate_only_once(
    tmp_path: Path,
) -> None:
    service = GatedService()
    first, store = _coordinator(tmp_path, service)
    second = ReviewScheduleCoordinator(
        service=service,  # type: ignore[arg-type]
        store=store,
        clock=lambda: AFTER_CUTOFF,
        settle_timeout=1.0,
    )

    async def scenario() -> None:
        running = asyncio.create_task(first.tick())
        await service.started.wait()
        assert await second.tick() is None
        assert service.calls == []
        service.gate.set()
        assert await running is not None
        assert len(service.calls) == 1

    asyncio.run(scenario())


def test_unreadable_state_does_not_kill_the_background_loop(
    tmp_path: Path,
) -> None:
    service = FakeService()
    store = ScheduleStore(tmp_path / "schedules")
    store.root.mkdir(parents=True, exist_ok=True)
    path = store.root / "review-schedule.json"
    path.write_text("{", encoding="utf-8")
    coordinator = ReviewScheduleCoordinator(
        service=service,  # type: ignore[arg-type]
        store=store,
        clock=lambda: AFTER_CUTOFF,
        poll_interval=0.05,
    )

    async def scenario() -> None:
        await coordinator.start()
        try:
            await asyncio.sleep(0.12)
            assert coordinator._task is not None
            assert not coordinator._task.done()
            assert service.calls == []
        finally:
            await coordinator.close()

    asyncio.run(scenario())


def test_execution_lock_defers_generation_until_the_runner_is_free(
    tmp_path: Path,
) -> None:
    service = FakeService()
    coordinator, store = _coordinator(tmp_path, service)
    lease = store.try_execution_lock()
    assert lease is not None
    try:
        assert asyncio.run(coordinator.tick()) is None
        assert service.calls == []
    finally:
        lease.release()

    assert asyncio.run(coordinator.tick()) is not None
    assert len(service.calls) == 1


def test_transient_store_failure_retries_without_advancing_the_day(
    tmp_path: Path,
) -> None:
    service = FakeService(fail=OSError("store unavailable"))
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        assert await coordinator.tick() is None
        assert coordinator.state.get("last_attempt_day") is None
        assert coordinator.state["next_retry_at"]
        assert await coordinator.tick() is None
        assert service.calls == []

    asyncio.run(scenario())


def test_policy_rejection_is_not_retried_in_a_loop(tmp_path: Path) -> None:
    service = FakeService(fail=RuntimeError("completed review has different input"))
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        assert await coordinator.tick() is None
        assert coordinator.state["last_attempt_day"] == "2026-01-15"
        assert await coordinator.tick() is None

    asyncio.run(scenario())


def test_state_file_is_private_and_restart_readable(tmp_path: Path) -> None:
    service = FakeService()
    coordinator, store = _coordinator(tmp_path, service)
    asyncio.run(coordinator.tick())
    path = store.root / "review-schedule.json"
    assert path.is_file()
    assert path.stat().st_mode & 0o077 == 0
    assert coordinator.state["last_attempt_day"] == "2026-01-15"
