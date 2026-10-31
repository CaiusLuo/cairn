"""Durable scheduled-task values and occurrence rules.

The values are host-authored: a model never creates a schedule. They are kept
small and serialisable so a JSON store can survive restarts without a database.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScheduleKind(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    INTERVAL = "interval"


class ScheduleJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    job_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    #: Ownership is host-authored at creation. Empty means legacy/unowned and
    #: must never be claimed or delivered by a guessed session.
    workspace: str = Field(default="", max_length=4096)
    recipient: str = Field(default="", max_length=256)
    account_id: str = Field(default="", max_length=256)
    kind: ScheduleKind
    at: datetime | None = None
    daily_time: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    created_at: datetime
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ScheduleJob:
        if self.kind is ScheduleKind.ONCE:
            if self.at is None or self.daily_time is not None:
                raise ValueError("once schedules require at and no daily_time")
        elif self.kind is ScheduleKind.DAILY:
            if self.daily_time is None:
                raise ValueError("daily schedules require daily_time")
            _parse_daily_time(self.daily_time)
        else:
            if self.interval_seconds is None:
                raise ValueError("interval schedules require interval_seconds")
        _zone(self.timezone)
        return self


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown timezone: {name}") from None


def _parse_daily_time(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError):
        raise ValueError("daily_time must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("daily_time must be HH:MM")
    return time(hour=hour, minute=minute)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule datetimes must include a timezone")
    return value


def initial_next_run(
    kind: ScheduleKind,
    *,
    now: datetime,
    at: datetime | None = None,
    daily_time: str | None = None,
    interval_seconds: int | None = None,
    timezone: str = "Asia/Shanghai",
) -> datetime:
    """The first wall-clock occurrence of a new job."""
    now = _require_aware(now)
    if kind is ScheduleKind.ONCE:
        assert at is not None
        return _require_aware(at)
    if kind is ScheduleKind.DAILY:
        assert daily_time is not None
        return _next_daily(daily_time, timezone, now)
    assert interval_seconds is not None
    return now + timedelta(seconds=interval_seconds)


def next_run_after(job: ScheduleJob, now: datetime) -> datetime | None:
    """The following occurrence, or None for a completed one-shot job."""
    now = _require_aware(now)
    if job.kind is ScheduleKind.ONCE:
        return None
    if job.kind is ScheduleKind.DAILY:
        assert job.daily_time is not None
        return _next_daily(job.daily_time, job.timezone, now)
    assert job.interval_seconds is not None
    return now + timedelta(seconds=job.interval_seconds)


def _next_daily(daily_time: str, timezone: str, now: datetime) -> datetime:
    zone = _zone(timezone)
    local_now = now.astimezone(zone)
    clock = _parse_daily_time(daily_time)
    candidate = datetime.combine(local_now.date(), clock, tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(
            local_now.date() + timedelta(days=1), clock, tzinfo=zone
        )
    return candidate
