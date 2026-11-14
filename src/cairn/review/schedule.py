"""Host-owned daily review schedule.

The schedule is a singleton host policy, not a user ScheduleJob. It reuses the
existing schedule directory and execution lock, but stores no job in the user
job list. Review generation depends on the model provider, never on WeChat
login; delivery is a separate outbox handled by ReviewDeliveryCoordinator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cairn.review.models import ReviewStatus, StoredReview
from cairn.review.service import DailyReviewService
from cairn.scheduling.store import ScheduleStore
from cairn.shared.atomic_json import atomic_write

REVIEW_TIMEZONE = "Asia/Shanghai"
REVIEW_CUTOFF = time(20, 30)
REVIEW_REQUEST_PREFIX = "host-daily-review"
REVIEW_SCHEDULE_VERSION = 1
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_SETTLE_SECONDS = 300.0
RETRY_SECONDS = 300.0


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
        self.path = store.root / "review-schedule.json"
        if self.path.is_symlink():
            raise ValueError("Review schedule state cannot be a symlink")
        self._state: dict[str, Any] | None = None
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> dict[str, Any]:
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": REVIEW_SCHEDULE_VERSION}
        except OSError:
            raise ValueError("review schedule state is unreadable") from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("review schedule state is not valid JSON") from None
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
                # The error may itself come from an unreadable state file, so a
                # failed diagnostic write must not terminate the loop.
                with contextlib.suppress(Exception):
                    self._save(last_error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)

    def due_day(self, now: datetime) -> date | None:
        """The most recent cutoff day, or None when it was already attempted."""
        local_now = now.astimezone(ZoneInfo(self.timezone))
        today_cutoff = datetime.combine(
            local_now.date(), self.cutoff, tzinfo=ZoneInfo(self.timezone)
        )
        latest = (
            local_now.date()
            if local_now >= today_cutoff
            else local_now.date() - timedelta(days=1)
        )
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

    async def tick(self) -> StoredReview | None:
        now = self.clock()
        day = self.due_day(now)
        if day is None:
            return None
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
                    deliver=True,
                )
            except (OSError, sqlite3.Error) as exc:
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
        self._save(last_status=settled.status.value)
        if settled.status is ReviewStatus.COMPLETED and self.on_settled is not None:
            result = self.on_settled(settled)
            if asyncio.iscoroutine(result):
                await result
        return settled

    def _request_id(self, day: date) -> str:
        return f"{REVIEW_REQUEST_PREFIX}:{day.isoformat()}:{self.timezone}"


__all__ = [
    "REVIEW_CUTOFF",
    "REVIEW_TIMEZONE",
    "ReviewScheduleCoordinator",
]
