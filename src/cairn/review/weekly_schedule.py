"""Host-owned weekly review schedule.

The weekly schedule is a singleton host policy with its own durable cursor, so
enabling daily and weekly reviews together never overwrites either schedule's
state. Generation depends on the model provider, never on WeChat login; only a
completed review leaves a delivery intent behind for the existing coordinator.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cairn.review.models import ReviewStatus, StoredReview
from cairn.review.weekly import (
    DEFAULT_WEEKLY_CUTOFF,
    WeeklyReviewService,
    WeekPeriod,
    iso_week_id,
    week_period,
    week_start,
)
from cairn.scheduling.store import ScheduleStore
from cairn.shared.atomic_json import atomic_write

REVIEW_WEEKLY_TIMEZONE = "Asia/Shanghai"
REVIEW_WEEKLY_TIME = time(20, 40)
REVIEW_WEEKLY_REQUEST_PREFIX = "host-weekly-review"
WEEKLY_SCHEDULE_VERSION = 1
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_SETTLE_SECONDS = 300.0
RETRY_SECONDS = 300.0
SUNDAY = 6
# A long shutdown generates only the most recent week. Older periods are
# recorded as skipped instead of pushing a burst of stale weekly messages.
MAX_CATCH_UP_WEEKS = 1
MAX_SKIPPED_WEEKS = 52
MAX_RETRY_RECEIPTS = 200


class WeeklyReviewScheduleCoordinator:
    """Generate one weekly report per local Sunday cutoff, at most once."""

    def __init__(
        self,
        *,
        service: WeeklyReviewService,
        store: ScheduleStore,
        on_settled: Callable[[StoredReview], object] | None = None,
        timezone: str = REVIEW_WEEKLY_TIMEZONE,
        cutoff: time = DEFAULT_WEEKLY_CUTOFF,
        scheduled_time: time = REVIEW_WEEKLY_TIME,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        settle_timeout: float = DEFAULT_SETTLE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.on_settled = on_settled
        self.timezone = timezone
        self.cutoff = cutoff
        self.scheduled_time = scheduled_time
        self.poll_interval = max(0.05, poll_interval)
        self.settle_timeout = settle_timeout
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.path = store.root / "weekly-review-schedule.json"
        if self.path.is_symlink():
            raise ValueError("Weekly review schedule state cannot be a symlink")
        self._state: dict[str, Any] | None = None
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._reconciled = False

    @property
    def state(self) -> dict[str, Any]:
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": WEEKLY_SCHEDULE_VERSION}
        except OSError:
            raise ValueError("weekly review schedule state is unreadable") from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("weekly review schedule state is not valid JSON") from None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != WEEKLY_SCHEDULE_VERSION
        ):
            raise ValueError("weekly review schedule state has an unsupported shape")
        return payload

    def _save(self, **updates: Any) -> None:
        state = dict(self.state)
        state.update(updates)
        state["version"] = WEEKLY_SCHEDULE_VERSION
        state["updated_at"] = self.clock().astimezone(ZoneInfo("UTC")).isoformat()
        atomic_write(self.path, state)
        self._state = state

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task is None or self._task.done():
            self._task = None
            return
        assert self._stop is not None
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with contextlib.suppress(Exception):
                    self._save(last_error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _scheduled_boundary(self, day: date) -> datetime:
        return datetime.combine(day, self.scheduled_time, tzinfo=self._zone())

    def latest_due_week(self, now: datetime) -> WeekPeriod:
        """The most recent completed Sunday boundary's seven-day window."""
        local_now = now.astimezone(self._zone())
        days_since_sunday = (local_now.weekday() - SUNDAY) % 7
        candidate = local_now.date() - timedelta(days=days_since_sunday)
        if local_now < self._scheduled_boundary(candidate):
            candidate -= timedelta(days=7)
        return week_period(
            iso_week_id(candidate), timezone=self.timezone, cutoff=self.cutoff
        )

    def _ensure_initialized(self, now: datetime) -> None:
        """A first enablement starts at the current week, never older history."""
        state = self.state
        if state.get("initialized"):
            return
        if "last_attempt_week" in state:
            self._save(
                initialized=True,
                enabled_at=(
                    state.get("enabled_at")
                    or state.get("updated_at")
                    or now.isoformat()
                ),
            )
            return
        local_now = now.astimezone(self._zone())
        latest = self.latest_due_week(now)
        updates: dict[str, Any] = {
            "initialized": True,
            "enabled_at": now.isoformat(),
        }
        days_until_sunday = (SUNDAY - local_now.weekday()) % 7
        current_sunday = local_now.date() + timedelta(days=days_until_sunday)
        if local_now < self._scheduled_boundary(current_sunday):
            # The next boundary is still ahead; the latest completed week is
            # history and must not be backfilled on first enablement.
            updates["last_attempt_week"] = latest.week_id
        self._save(**updates)

    def pending_weeks(self, now: datetime) -> tuple[WeekPeriod, ...]:
        """Weekly periods after the persisted cursor, oldest first."""
        latest = self.latest_due_week(now)
        attempted = self.state.get("last_attempt_week")
        if not isinstance(attempted, str):
            return (latest,)
        try:
            cursor_day = week_start(attempted)
        except ValueError:
            return (latest,)
        weeks: list[WeekPeriod] = []
        current = cursor_day + timedelta(days=7)
        while current <= latest.day:
            weeks.append(
                week_period(
                    iso_week_id(current), timezone=self.timezone, cutoff=self.cutoff
                )
            )
            current += timedelta(days=7)
        return tuple(weeks)

    def _bounded_pending(self, weeks: tuple[WeekPeriod, ...]) -> tuple[WeekPeriod, ...]:
        if len(weeks) <= MAX_CATCH_UP_WEEKS:
            return weeks
        skipped = weeks[:-MAX_CATCH_UP_WEEKS]
        recorded = list(self.state.get("skipped_weeks") or [])
        recorded.extend(week.week_id for week in skipped)
        self._save(
            skipped_weeks=recorded[-MAX_SKIPPED_WEEKS:],
            last_attempt_week=skipped[-1].week_id,
        )
        return weeks[-MAX_CATCH_UP_WEEKS:]

    def _record_settled(self, period: WeekPeriod, settled: StoredReview) -> None:
        if settled.status is not ReviewStatus.FAILED:
            self._save(last_status=settled.status.value)
            return
        failed = dict(self.state.get("failed_weeks") or {})
        failed[period.week_id] = {
            "review_id": settled.review_id,
            "error": settled.error or "generation_failed",
            "at": self.clock().isoformat(),
        }
        self._save(
            last_status=settled.status.value,
            last_error=settled.error,
            failed_weeks=failed,
        )

    async def _reconcile_interrupted(self) -> None:
        """Turn a claimed-but-crashed week into an auditable failed entry."""
        if self._reconciled:
            return
        state = self.state
        self._reconciled = True
        week_id = state.get("last_attempt_week")
        review_id = state.get("last_review_id")
        status = state.get("last_status")
        if not (
            isinstance(week_id, str)
            and isinstance(review_id, str)
            and status in {"pending", "generating"}
        ):
            return
        try:
            review = await self.service.get_review(review_id)
        except (OSError, sqlite3.Error):
            self._reconciled = False
            return
        if review is None:
            return
        if review.status is ReviewStatus.FAILED:
            failed = dict(state.get("failed_weeks") or {})
            failed[week_id] = {
                "review_id": review_id,
                "error": review.error or "interrupted",
                "at": self.clock().isoformat(),
            }
            self._save(
                last_status=review.status.value,
                last_error=review.error,
                failed_weeks=failed,
            )
        elif review.status is ReviewStatus.COMPLETED:
            self._save(last_status=review.status.value)

    async def tick(self) -> StoredReview | None:
        now = self.clock()
        self._ensure_initialized(now)
        await self._reconcile_interrupted()
        retry_at = self.state.get("next_retry_at")
        if isinstance(retry_at, str):
            try:
                if now < datetime.fromisoformat(retry_at):
                    return None
            except ValueError:
                pass
        weeks = self._bounded_pending(self.pending_weeks(now))
        if not weeks:
            return None
        period = weeks[0]
        # Only the current week may notify WeChat; compensating an older period
        # must never produce a burst of stale weekly messages.
        deliver = period.week_id == self.latest_due_week(now).week_id
        lease = self.store.try_execution_lock()
        if lease is None:
            return None
        try:
            try:
                review = await self.service.generate_weekly_review(
                    period.week_id,
                    self._request_id(period),
                    timezone=self.timezone,
                    period_end=period.period_end,
                    deliver=deliver,
                    reuse_completed=True,
                )
            except (OSError, sqlite3.Error) as exc:
                self._save(
                    last_error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=(now + timedelta(seconds=RETRY_SECONDS)).isoformat(),
                )
                return None
            except Exception as exc:
                self._save(
                    last_attempt_week=period.week_id,
                    last_status="error",
                    last_error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=None,
                )
                return None
            self._save(
                last_attempt_week=period.week_id,
                last_review_id=review.review_id,
                last_status=review.status.value,
                last_error=None,
                next_retry_at=None,
            )
        finally:
            lease.release()

        try:
            settled = await self.service.wait_for_review(
                review.review_id, timeout=self.settle_timeout
            )
        except TimeoutError:
            self._save(last_status="generating")
            return review
        self._record_settled(period, settled)
        if settled.status is ReviewStatus.COMPLETED:
            if deliver:
                await self.service.ensure_delivery(settled.review_id)
            if self.on_settled is not None:
                result = self.on_settled(settled)
                if asyncio.iscoroutine(result):
                    await result
        return settled

    async def retry_failed(self, week_id: str, request_id: str) -> StoredReview | None:
        """Explicit, idempotent operator retry for one auditable failed week."""
        key = request_id.strip()
        if not key:
            raise ValueError("request_id must not be blank")
        now = self.clock()
        self._ensure_initialized(now)
        await self._reconcile_interrupted()
        receipts = dict(self.state.get("retry_requests") or {})
        receipt = receipts.get(key)
        if receipt is not None:
            if receipt.get("week_id") != week_id:
                raise ValueError("retry request_id was used for another week")
            review_id = receipt.get("review_id")
            review = (
                None
                if not isinstance(review_id, str)
                else await self.service.get_review(review_id)
            )
            if review is None:
                raise ValueError("retry receipt points at a missing review")
            if (
                review.status is ReviewStatus.COMPLETED
                and week_id == self.latest_due_week(now).week_id
            ):
                await self.service.ensure_delivery(review.review_id)
            return review
        failed = dict(self.state.get("failed_weeks") or {})
        if week_id not in failed:
            return None
        period = week_period(week_id, timezone=self.timezone, cutoff=self.cutoff)
        deliver = week_id == self.latest_due_week(now).week_id
        lease = self.store.try_execution_lock()
        if lease is None:
            raise RuntimeError("review schedule is busy")
        try:
            review = await self.service.generate_weekly_review(
                week_id,
                self._retry_request_id(week_id, key),
                timezone=self.timezone,
                regenerate=True,
                period_end=period.period_end,
                deliver=deliver,
            )
            receipts[key] = {
                "week_id": week_id,
                "review_id": review.review_id,
                "at": self.clock().isoformat(),
            }
            self._save(
                retry_requests=dict(list(receipts.items())[-MAX_RETRY_RECEIPTS:])
            )
        finally:
            lease.release()
        try:
            settled = await self.service.wait_for_review(
                review.review_id, timeout=self.settle_timeout
            )
        except TimeoutError:
            self._save(last_status="generating")
            return review
        if settled.status is ReviewStatus.COMPLETED:
            failed.pop(week_id, None)
            retried = dict(self.state.get("retried_weeks") or {})
            retried[week_id] = {
                "review_id": settled.review_id,
                "at": self.clock().isoformat(),
            }
            self._save(
                last_status=settled.status.value,
                last_error=None,
                failed_weeks=failed,
                retried_weeks=retried,
            )
            if deliver:
                await self.service.ensure_delivery(settled.review_id)
            if self.on_settled is not None:
                result = self.on_settled(settled)
                if asyncio.iscoroutine(result):
                    await result
        else:
            self._record_settled(period, settled)
        return settled

    def _request_id(self, period: WeekPeriod) -> str:
        return (
            f"{REVIEW_WEEKLY_REQUEST_PREFIX}:{period.week_id}:{self.timezone}:"
            f"{self.scheduled_time.strftime('%H:%M')}"
        )

    @staticmethod
    def _retry_request_id(week_id: str, request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        return f"{REVIEW_WEEKLY_REQUEST_PREFIX}:retry:{week_id}:{digest}"


__all__ = [
    "REVIEW_WEEKLY_TIME",
    "REVIEW_WEEKLY_TIMEZONE",
    "WeeklyReviewScheduleCoordinator",
]
