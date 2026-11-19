"""Host-owned daily review schedule.

The schedule is a singleton host policy, not a user ScheduleJob. It reuses the
existing schedule directory and execution lock, but stores no job in the user
job list. Review generation depends on the model provider, never on WeChat
login; delivery is a separate outbox handled by ReviewDeliveryCoordinator.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import Error as PostgresError

from cairn.review.models import ReviewStatus, StoredReview
from cairn.review.service import DailyReviewService
from cairn.scheduling.store import ScheduleStore

REVIEW_TIMEZONE = "Asia/Shanghai"
REVIEW_CUTOFF = time(20, 30)
REVIEW_REQUEST_PREFIX = "host-daily-review"
REVIEW_SCHEDULE_VERSION = 1
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_SETTLE_SECONDS = 300.0
RETRY_SECONDS = 300.0
# A long shutdown compensates at most this many unclaimed cutoff days. Older
# periods are recorded as skipped instead of generating a burst of stale
# reports or sending multiple overdue WeChat messages.
MAX_CATCH_UP_DAYS = 3
MAX_SKIPPED_DAYS = 31
MAX_RETRY_RECEIPTS = 200


class ReviewScheduleCoordinator:
    """Generate one review per local cutoff day, at most once per day."""

    def __init__(
        self,
        *,
        service: DailyReviewService,
        store: ScheduleStore,
        on_settled: Callable[[StoredReview], object] | None = None,
        timezone: str = REVIEW_TIMEZONE,
        cutoff: time = REVIEW_CUTOFF,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        settle_timeout: float = DEFAULT_SETTLE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.on_settled = on_settled
        self.timezone = timezone
        self.cutoff = cutoff
        self.poll_interval = max(0.05, poll_interval)
        self.settle_timeout = settle_timeout
        self.clock = clock or (lambda: datetime.now().astimezone())
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
        payload = self.store.load_state("review-schedule")
        if payload is None:
            return {"version": REVIEW_SCHEDULE_VERSION}
        if (
            not isinstance(payload, dict)
            or payload.get("version") != REVIEW_SCHEDULE_VERSION
        ):
            raise ValueError("review schedule state has an unsupported shape")
        return payload

    def _save(self, **updates: Any) -> None:
        state = dict(self.state)
        state.update(updates)
        state["version"] = REVIEW_SCHEDULE_VERSION
        state["updated_at"] = self.clock().astimezone(ZoneInfo("UTC")).isoformat()
        self.store.save_state("review-schedule", state)
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
                # The error may itself come from an unreadable state file, so a
                # failed diagnostic write must not terminate the loop.
                with contextlib.suppress(Exception):
                    self._save(last_error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)

    def _ensure_initialized(self, now: datetime) -> None:
        """Separate a first enablement from a restart with an existing cursor.

        Enabling the schedule before today's cutoff must not immediately
        generate and send yesterday's report. A first enablement therefore
        starts its cursor at yesterday, so the first report covers the period
        ending at today's cutoff. A restart keeps its persisted cursor.
        """
        state = self.state
        if state.get("initialized"):
            return
        if "last_attempt_day" in state:
            # State written before activation tracking existed is a restart,
            # not a fresh enablement.
            self._save(
                initialized=True,
                enabled_at=(
                    state.get("enabled_at")
                    or state.get("updated_at")
                    or now.isoformat()
                ),
            )
            return
        local_now = now.astimezone(ZoneInfo(self.timezone))
        today_cutoff = datetime.combine(
            local_now.date(), self.cutoff, tzinfo=ZoneInfo(self.timezone)
        )
        updates: dict[str, Any] = {
            "initialized": True,
            "enabled_at": now.isoformat(),
        }
        if local_now < today_cutoff:
            updates["last_attempt_day"] = (
                local_now.date() - timedelta(days=1)
            ).isoformat()
        self._save(**updates)

    def latest_due_day(self, now: datetime) -> date:
        """The most recent completed cutoff boundary."""
        local_now = now.astimezone(ZoneInfo(self.timezone))
        today_cutoff = datetime.combine(
            local_now.date(), self.cutoff, tzinfo=ZoneInfo(self.timezone)
        )
        return (
            local_now.date()
            if local_now >= today_cutoff
            else local_now.date() - timedelta(days=1)
        )

    def due_day(self, now: datetime) -> date | None:
        """The most recent cutoff day, or None when it was already attempted."""
        latest = self.latest_due_day(now)
        attempted = self.state.get("last_attempt_day")
        if isinstance(attempted, str) and attempted >= latest.isoformat():
            return None
        retry_at = self.state.get("next_retry_at")
        if isinstance(retry_at, str):
            try:
                if now < datetime.fromisoformat(retry_at):
                    return None
            except ValueError:
                pass
        return latest

    def pending_days(self, now: datetime) -> tuple[date, ...]:
        """Cutoff days after the persisted cursor, oldest first."""
        latest = self.latest_due_day(now)
        attempted = self.state.get("last_attempt_day")
        if not isinstance(attempted, str):
            return (latest,)
        try:
            cursor = date.fromisoformat(attempted)
        except ValueError:
            return (latest,)
        days: list[date] = []
        current = cursor + timedelta(days=1)
        while current <= latest:
            days.append(current)
            current += timedelta(days=1)
        return tuple(days)

    def _bounded_pending(self, days: tuple[date, ...]) -> tuple[date, ...]:
        if len(days) <= MAX_CATCH_UP_DAYS:
            return days
        skipped = days[:-MAX_CATCH_UP_DAYS]
        recorded = list(self.state.get("skipped_days") or [])
        recorded.extend(day.isoformat() for day in skipped)
        self._save(
            skipped_days=recorded[-MAX_SKIPPED_DAYS:],
            last_attempt_day=skipped[-1].isoformat(),
        )
        return days[-MAX_CATCH_UP_DAYS:]

    def _record_settled(self, day: date, settled: StoredReview) -> None:
        if settled.status is not ReviewStatus.FAILED:
            self._save(last_status=settled.status.value)
            return
        failed = dict(self.state.get("failed_days") or {})
        failed[day.isoformat()] = {
            "review_id": settled.review_id,
            "error": settled.error or "generation_failed",
            "at": self.clock().isoformat(),
        }
        self._save(
            last_status=settled.status.value,
            last_error=settled.error,
            failed_days=failed,
        )

    async def _reconcile_interrupted(self) -> None:
        """Turn a claimed-but-crashed period into an auditable failed entry."""
        if self._reconciled:
            return
        state = self.state
        self._reconciled = True
        day = state.get("last_attempt_day")
        review_id = state.get("last_review_id")
        status = state.get("last_status")
        if not (
            isinstance(day, str)
            and isinstance(review_id, str)
            and status in {"pending", "generating"}
        ):
            return
        getter = getattr(self.service, "get_review", None)
        if getter is None:
            return
        try:
            review = await getter(review_id)
        except (OSError, PostgresError):
            self._reconciled = False
            return
        if review is None:
            return
        if review.status is ReviewStatus.FAILED:
            failed = dict(state.get("failed_days") or {})
            failed[day] = {
                "review_id": review_id,
                "error": review.error or "interrupted",
                "at": self.clock().isoformat(),
            }
            self._save(
                last_status=review.status.value,
                last_error=review.error,
                failed_days=failed,
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
        days = self._bounded_pending(self.pending_days(now))
        if not days:
            return None
        day = days[0]
        # Only the current period may notify WeChat; compensating older
        # periods must not produce a burst of stale notifications.
        deliver = day == self.latest_due_day(now)
        lease = self.store.try_execution_lock()
        if lease is None:
            # An ordinary ScheduleJob is executing; retry on the next tick.
            return None
        try:
            try:
                review = await self.service.generate_daily_review(
                    day,
                    self._request_id(day),
                    timezone=self.timezone,
                    cutoff=self.cutoff,
                    deliver=deliver,
                )
            except (OSError, PostgresError) as exc:
                # A temporarily unavailable store is retried later; a policy
                # rejection is not retried in a loop for the same cutoff day.
                self._save(
                    last_error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=(now + timedelta(seconds=RETRY_SECONDS)).isoformat(),
                )
                return None
            except Exception as exc:
                self._save(
                    last_attempt_day=day.isoformat(),
                    last_status="error",
                    last_error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=None,
                )
                return None
            self._save(
                last_attempt_day=day.isoformat(),
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
        self._record_settled(day, settled)
        if settled.status is ReviewStatus.COMPLETED and self.on_settled is not None:
            result = self.on_settled(settled)
            if asyncio.iscoroutine(result):
                await result
        return settled

    async def retry_failed(
        self, day: date, request_id: str | None = None
    ) -> StoredReview | None:
        """Explicit operator retry for one auditable failed period.

        The cursor is not moved: this is a new review version for the same
        period, using a fresh request id as the operator's new confirmation.
        When the caller supplies a request id, it is stable and hashed into the
        scheduled request identity so a lost HTTP response can be retried
        without a second model call or version. A terminal receipt keeps the
        original outcome replayable even after the failed day is cleared.
        """
        key = None if request_id is None else request_id.strip()
        if key == "":
            raise ValueError("request_id must not be blank")
        now = self.clock()
        self._ensure_initialized(now)
        await self._reconcile_interrupted()
        receipts = dict(self.state.get("retry_requests") or {})
        receipt = receipts.get(key) if key is not None else None
        if receipt is not None:
            if receipt.get("day") != day.isoformat():
                raise ValueError("retry request_id was used for another day")
            review_id = receipt.get("review_id")
            review = (
                None
                if not isinstance(review_id, str)
                else await self.service.get_review(review_id)
            )
            if review is None:
                raise ValueError("retry receipt points at a missing review")
            if review.status in {ReviewStatus.PENDING, ReviewStatus.GENERATING}:
                try:
                    review = await self.service.wait_for_review(
                        review.review_id, timeout=self.settle_timeout
                    )
                except TimeoutError:
                    return review
            return review
        failed = dict(self.state.get("failed_days") or {})
        if day.isoformat() not in failed:
            return None
        lease = self.store.try_execution_lock()
        if lease is None:
            raise RuntimeError("review schedule is busy")
        try:
            review = await self.service.generate_daily_review(
                day,
                (
                    f"{self._request_id(day)}:retry:"
                    + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
                    if key is not None
                    else f"{self._request_id(day)}:retry:{uuid.uuid4().hex[:12]}"
                ),
                timezone=self.timezone,
                regenerate=True,
                cutoff=self.cutoff,
                deliver=day == self.latest_due_day(now),
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
        if key is not None:
            receipts[key] = {
                "day": day.isoformat(),
                "review_id": settled.review_id,
                "at": self.clock().isoformat(),
            }
            self._save(
                retry_requests=dict(list(receipts.items())[-MAX_RETRY_RECEIPTS:])
            )
        if settled.status is ReviewStatus.COMPLETED:
            failed.pop(day.isoformat(), None)
            retried = dict(self.state.get("retried_days") or {})
            retried[day.isoformat()] = {
                "review_id": settled.review_id,
                "at": self.clock().isoformat(),
            }
            self._save(
                last_status=settled.status.value,
                last_error=None,
                failed_days=failed,
                retried_days=retried,
            )
            if self.on_settled is not None:
                result = self.on_settled(settled)
                if asyncio.iscoroutine(result):
                    await result
        else:
            self._record_settled(day, settled)
        return settled

    def _request_id(self, day: date) -> str:
        return (
            f"{REVIEW_REQUEST_PREFIX}:{day.isoformat()}:{self.timezone}:"
            f"{self.cutoff.strftime('%H:%M')}"
        )


__all__ = [
    "REVIEW_CUTOFF",
    "REVIEW_TIMEZONE",
    "ReviewScheduleCoordinator",
]
