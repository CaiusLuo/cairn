"""The weekly host schedule has its own cursor and never spams stale weeks."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cairn.knowledge.store import KnowledgeStore
from cairn.review.models import (
    DeliveryTarget,
    ReviewDeliveryStatus,
    ReviewInput,
    ReviewStatus,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
)
from cairn.review.store import ReviewStore
from cairn.review.weekly import WeeklyReviewService, week_period
from cairn.review.weekly_schedule import WeeklyReviewScheduleCoordinator
from cairn.scheduling.store import ScheduleStore
from tests.support.pg import database_for

TZ = "Asia/Shanghai"
AFTER_BOUNDARY = datetime(2026, 1, 18, 20, 41, tzinfo=ZoneInfo(TZ))
BEFORE_BOUNDARY = datetime(2026, 1, 18, 20, 39, tzinfo=ZoneInfo(TZ))
WEDNESDAY = datetime(2026, 1, 14, 12, 0, tzinfo=ZoneInfo(TZ))
MONDAY = datetime(2026, 1, 19, 9, 0, tzinfo=ZoneInfo(TZ))


class FakeWeeklyService:
    def __init__(self, *, status: ReviewStatus = ReviewStatus.COMPLETED) -> None:
        self.status = status
        self.error: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.settled: list[Any] = []
        self.delivered: list[str] = []
        self._counter = 0
        self._reviews: dict[str, Any] = {}

    async def generate_weekly_review(
        self,
        week_id: str,
        request_id: str,
        *,
        timezone: str = TZ,
        regenerate: bool = False,
        period_end: datetime | None = None,
        deliver: bool = False,
        reuse_completed: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "week_id": week_id,
                "request_id": request_id,
                "timezone": timezone,
                "regenerate": regenerate,
                "period_end": period_end,
                "deliver": deliver,
                "reuse_completed": reuse_completed,
            }
        )
        self._counter += 1
        record = SimpleNamespace(
            review_id=f"review-{self._counter}", status=ReviewStatus.PENDING
        )
        self._reviews[record.review_id] = record
        return record

    async def wait_for_review(self, review_id: str, *, timeout: float) -> Any:
        record = SimpleNamespace(
            review_id=review_id,
            status=self.status,
            error=self.error,
        )
        self._reviews[review_id] = record
        self.settled.append(record)
        return record

    async def get_review(self, review_id: str) -> Any:
        return self._reviews.get(review_id)

    async def ensure_delivery(self, review_id: str) -> None:
        self.delivered.append(review_id)


def _coordinator(
    tmp_path: Path,
    service: FakeWeeklyService,
    *,
    clock: datetime = AFTER_BOUNDARY,
    store: ScheduleStore | None = None,
    on_settled: Any = None,
) -> tuple[WeeklyReviewScheduleCoordinator, ScheduleStore]:
    schedule_store = store or ScheduleStore(database_for(tmp_path))
    coordinator = WeeklyReviewScheduleCoordinator(
        service=service,  # type: ignore[arg-type]
        store=schedule_store,
        clock=lambda: clock,
        on_settled=on_settled,
        settle_timeout=1.0,
    )
    return coordinator, schedule_store


def _seed_state(tmp_path: Path, **fields: Any) -> ScheduleStore:
    store = ScheduleStore(database_for(tmp_path))
    payload: dict[str, Any] = {
        "version": 1,
        "initialized": True,
        "enabled_at": "2026-01-01T12:00:00+08:00",
    }
    payload.update(fields)
    store.save_state("weekly-review-schedule", payload)
    return store


def test_latest_due_week_uses_the_sunday_2040_boundary(tmp_path: Path) -> None:
    service = FakeWeeklyService()
    coordinator, _ = _coordinator(tmp_path, service)
    for moment, expected in (
        (AFTER_BOUNDARY, "2026-W03"),
        (BEFORE_BOUNDARY, "2026-W02"),
        (MONDAY, "2026-W03"),
    ):
        assert coordinator.latest_due_week(moment).week_id == expected
    period = coordinator.latest_due_week(AFTER_BOUNDARY)
    assert period.period_start.isoformat() == "2026-01-11T20:30:00+08:00"
    assert period.period_end.isoformat() == "2026-01-18T20:30:00+08:00"


def test_first_activation_before_the_boundary_does_not_backfill(tmp_path: Path) -> None:
    service = FakeWeeklyService()
    coordinator, _ = _coordinator(tmp_path, service, clock=WEDNESDAY)

    async def scenario() -> None:
        assert await coordinator.tick() is None
        assert service.calls == []
        assert coordinator.state["initialized"] is True
        assert coordinator.state["last_attempt_week"] == "2026-W02"

    asyncio.run(scenario())


def test_first_activation_after_the_boundary_generates_the_just_ended_week(
    tmp_path: Path,
) -> None:
    service = FakeWeeklyService()
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert service.calls[0]["week_id"] == "2026-W03"
        assert coordinator.state["last_attempt_week"] == "2026-W03"

    asyncio.run(scenario())


def test_tick_generates_once_and_delivers_only_the_current_week(tmp_path: Path) -> None:
    service = FakeWeeklyService()
    settled: list[Any] = []
    coordinator, _ = _coordinator(tmp_path, service, on_settled=settled.append)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert service.calls == [
            {
                "week_id": "2026-W03",
                "request_id": ("host-weekly-review:2026-W03:Asia/Shanghai:20:40"),
                "timezone": TZ,
                "regenerate": False,
                "period_end": week_period("2026-W03", timezone=TZ).period_end,
                "deliver": True,
                "reuse_completed": True,
            }
        ]
        assert [row.status for row in settled] == [ReviewStatus.COMPLETED]
        assert service.delivered == ["review-1"]
        assert await coordinator.tick() is None
        assert len(service.calls) == 1

    asyncio.run(scenario())


def test_restart_uses_the_persisted_week_cursor(tmp_path: Path) -> None:
    service = FakeWeeklyService()
    coordinator, store = _coordinator(tmp_path, service)
    asyncio.run(coordinator.tick())
    assert len(service.calls) == 1

    restarted_service = FakeWeeklyService()
    restarted, _ = _coordinator(tmp_path, restarted_service, store=store)
    asyncio.run(restarted.tick())
    assert restarted_service.calls == []


def test_long_outage_generates_only_the_latest_week(tmp_path: Path) -> None:
    service = FakeWeeklyService()
    store = _seed_state(tmp_path, last_attempt_week="2025-W52")
    coordinator, _ = _coordinator(tmp_path, service, store=store)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert [call["week_id"] for call in service.calls] == ["2026-W03"]
        assert service.calls[0]["deliver"] is True
        assert coordinator.state["skipped_weeks"] == ["2026-W01", "2026-W02"]

    asyncio.run(scenario())


def test_failed_week_is_manual_to_retry_and_a_retry_is_new_version(
    tmp_path: Path,
) -> None:
    service = FakeWeeklyService(status=ReviewStatus.FAILED)
    service.error = "model_timeout"
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        review = await coordinator.tick()
        assert review is not None
        assert coordinator.state["failed_weeks"]["2026-W03"]["error"] == "model_timeout"
        assert await coordinator.tick() is None

        service.status = ReviewStatus.COMPLETED
        retried = await coordinator.retry_failed("2026-W03", "operator-retry-1")
        assert retried is not None
        assert service.calls[-1]["regenerate"] is True
        assert "retry:" in service.calls[-1]["request_id"]
        assert "2026-W03" not in coordinator.state["failed_weeks"]
        assert "2026-W03" in coordinator.state["retried_weeks"]
        assert await coordinator.retry_failed("2026-W03", "operator-retry-1") is retried
        assert len(service.calls) == 2
        assert await coordinator.retry_failed("2026-W03", "operator-retry-2") is None
        with pytest.raises(ValueError, match="another week"):
            await coordinator.retry_failed("2026-W04", "operator-retry-1")

    asyncio.run(scenario())


class FlakyWeeklyService(FakeWeeklyService):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    async def generate_weekly_review(self, *args: Any, **kwargs: Any) -> Any:
        if self.broken:
            raise RuntimeError("corrupt daily review row")
        return await super().generate_weekly_review(*args, **kwargs)


class TimeoutRetryService(FakeWeeklyService):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_next = False

    async def wait_for_review(self, review_id: str, *, timeout: float) -> Any:
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("generation did not settle")
        return await super().wait_for_review(review_id, timeout=timeout)


def test_unexpected_tick_failure_keeps_the_week_recoverable(tmp_path: Path) -> None:
    service = FlakyWeeklyService()
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        assert await coordinator.tick() is None
        failed = coordinator.state["failed_weeks"]["2026-W03"]
        assert "corrupt daily review row" in failed["error"]
        assert coordinator.state["last_attempt_week"] == "2026-W03"

        service.broken = False
        retried = await coordinator.retry_failed("2026-W03", "operator-recover-1")
        assert retried is not None
        assert "2026-W03" not in coordinator.state["failed_weeks"]

    asyncio.run(scenario())


def test_retry_timeout_does_not_persist_a_receipt(tmp_path: Path) -> None:
    service = TimeoutRetryService()
    service.status = ReviewStatus.FAILED
    service.error = "model_timeout"
    coordinator, _ = _coordinator(tmp_path, service)

    async def scenario() -> None:
        await coordinator.tick()
        assert "2026-W03" in coordinator.state["failed_weeks"]

        service.status = ReviewStatus.COMPLETED
        service.timeout_next = True
        first = await coordinator.retry_failed("2026-W03", "operator-timeout-1")
        assert first is not None
        assert first.status is ReviewStatus.PENDING
        assert not coordinator.state.get("retry_requests")
        assert "2026-W03" in coordinator.state["failed_weeks"]
        # A restart reconciles the retry attempt, not the older scheduled row.
        assert coordinator.state["last_review_id"] == first.review_id

        second = await coordinator.retry_failed("2026-W03", "operator-timeout-1")
        assert second is not None
        assert second.status is ReviewStatus.COMPLETED
        assert service.calls[-1]["request_id"] == service.calls[-2]["request_id"]
        assert "2026-W03" not in coordinator.state["failed_weeks"]
        assert coordinator.state["retry_requests"]["operator-timeout-1"]["review_id"]

    asyncio.run(scenario())


def test_weekly_schedule_state_is_separate_from_the_daily_schedule(
    tmp_path: Path,
) -> None:
    store = _seed_state(tmp_path)
    daily_state = {"version": 1, "last_attempt_day": "2026-01-17"}
    store.save_state("review-schedule", daily_state)
    coordinator, _ = _coordinator(tmp_path, FakeWeeklyService(), store=store)

    async def scenario() -> None:
        await coordinator.tick()
        assert store.load_state("review-schedule") == daily_state

    asyncio.run(scenario())


class EmptyWeeklyCollector:
    def collect_weekly(self, period: Any, timezone: str) -> ReviewInput:
        return ReviewInput.build(
            review_type=ReviewType.WEEKLY,
            day=period.day,
            timezone=timezone,
            period_start=period.period_start,
            period_end=period.period_end,
            sources=(),
            collection_status=(
                SourceCollectionStatus(
                    source_type="daily_review",
                    status=SourceCollectionState.EMPTY,
                    discovered=7,
                    included=0,
                    omitted=7,
                ),
            ),
            budget_chars=100_000,
        )


def test_scheduled_weekly_review_completes_without_wechat_login(
    tmp_path: Path,
) -> None:
    reviews = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    service = WeeklyReviewService(
        store=reviews,
        collector=EmptyWeeklyCollector(),
        llm=None,
        delivery_target=DeliveryTarget(recipient="owner"),
    )
    coordinator = WeeklyReviewScheduleCoordinator(
        service=service,
        store=ScheduleStore(database_for(tmp_path)),
        clock=lambda: AFTER_BOUNDARY,
        settle_timeout=1.0,
    )

    review = asyncio.run(coordinator.tick())
    assert review is not None
    assert review.status is ReviewStatus.COMPLETED
    delivery = reviews.get_delivery(review.review_id)
    assert delivery is not None
    assert delivery.status is ReviewDeliveryStatus.PENDING


def test_schedule_delivers_a_manual_report_created_before_the_tick(
    tmp_path: Path,
) -> None:
    reviews = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    service = WeeklyReviewService(
        store=reviews,
        collector=EmptyWeeklyCollector(),
        llm=None,
        delivery_target=DeliveryTarget(recipient="owner"),
    )
    manual = asyncio.run(
        service.generate_weekly_review("2026-W03", "manual-weekly", timezone=TZ)
    )
    assert reviews.get_delivery(manual.review_id) is None

    coordinator = WeeklyReviewScheduleCoordinator(
        service=service,
        store=ScheduleStore(database_for(tmp_path)),
        clock=lambda: AFTER_BOUNDARY,
        settle_timeout=1.0,
    )
    settled = asyncio.run(coordinator.tick())
    assert settled is not None
    assert settled.review_id == manual.review_id
    delivery = reviews.get_delivery(manual.review_id)
    assert delivery is not None
    assert delivery.status is ReviewDeliveryStatus.PENDING


def test_week_id_is_derived_from_the_calendar_not_the_cursor(tmp_path: Path) -> None:
    period = week_period("2026-W01", timezone=TZ)
    assert period.day == date(2026, 1, 4)
    assert period.period_start.year == 2025
    assert period.period_start.month == 12
