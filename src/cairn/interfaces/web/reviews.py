"""Authenticated review history and non-blocking daily generation API."""

from __future__ import annotations

import asyncio
import json
from datetime import date, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Error as PostgresError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.interfaces.web.auth import operator_request
from cairn.review.delivery import delivery_payload
from cairn.review.models import (
    REVIEW_REQUEST_ID_MAX_LENGTH,
    REVIEW_TIMEZONE_MAX_LENGTH,
    ReviewDeliveryStatus,
    ReviewType,
    StoredReview,
    StoredReviewDelivery,
    WeeklyReview,
)
from cairn.review.service import DailyReviewService, ReviewNoContent
from cairn.review.store import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ReviewPeriodCompleted,
    ReviewRequestConflict,
)
from cairn.review.weekly import (
    ReviewPeriodNotEnded,
    WeeklyReviewService,
    iso_week_id,
    week_period,
)

MAX_BODY_BYTES = 16 * 1024


def _json_headers() -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_json_headers())


async def _read_limited(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ValueError("review request is too large")
        body.extend(chunk)
    return bytes(body)


async def _body(request: Request) -> object:
    return json.loads(await _read_limited(request))


class DeliveryResendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=REVIEW_REQUEST_ID_MAX_LENGTH)
    rebind: bool = False
    # A delivery_unknown send may already have reached the recipient. The
    # operator must acknowledge that duplicate risk at the API boundary, not
    # only in the browser control that happens to disable the button.
    confirmed: bool = False

    @field_validator("request_id")
    @classmethod
    def _request_id_has_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


class ScheduleRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    # Optional operator request identity. Host schedules that predate it keep
    # their existing behaviour; the Web console sends one so a lost response
    # can be retried without creating a second review version.
    request_id: str | None = Field(
        default=None, min_length=1, max_length=REVIEW_REQUEST_ID_MAX_LENGTH
    )

    @field_validator("request_id")
    @classmethod
    def _request_id_has_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


class WeeklyScheduleRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_id: str = Field(min_length=1, max_length=10)
    request_id: str = Field(min_length=1, max_length=REVIEW_REQUEST_ID_MAX_LENGTH)

    @field_validator("week_id")
    @classmethod
    def _week_id_is_valid(cls, value: str) -> str:
        stripped = value.strip()
        try:
            week_period(stripped, timezone="UTC")
        except ValueError:
            raise ValueError("week_id must be a valid ISO week (YYYY-Www)") from None
        return stripped

    @field_validator("request_id")
    @classmethod
    def _request_id_has_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


class WeeklyReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_id: str = Field(min_length=1, max_length=10)
    timezone: str = Field(
        default="Asia/Shanghai",
        min_length=1,
        max_length=REVIEW_TIMEZONE_MAX_LENGTH,
    )
    request_id: str = Field(min_length=1, max_length=REVIEW_REQUEST_ID_MAX_LENGTH)
    regenerate: bool = False

    @field_validator("week_id")
    @classmethod
    def _week_id_is_valid(cls, value: str) -> str:
        stripped = value.strip()
        try:
            week_period(stripped, timezone="UTC")
        except ValueError:
            raise ValueError("week_id must be a valid ISO week (YYYY-Www)") from None
        return stripped

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("timezone must not be blank")
        try:
            ZoneInfo(stripped)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return stripped

    @field_validator("request_id")
    @classmethod
    def _request_id_has_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


class DailyReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    timezone: str = Field(
        default="Asia/Shanghai",
        min_length=1,
        max_length=REVIEW_TIMEZONE_MAX_LENGTH,
    )
    request_id: str = Field(min_length=1, max_length=REVIEW_REQUEST_ID_MAX_LENGTH)
    regenerate: bool = False

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("timezone must not be blank")
        try:
            ZoneInfo(stripped)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return stripped

    @field_validator("request_id")
    @classmethod
    def _request_id_has_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


def _summary_payload(record: StoredReview) -> dict[str, object]:
    payload: dict[str, object] = {
        "review_id": record.review_id,
        "review_type": record.review_type.value,
        "date": record.date.isoformat(),
        "period_start": record.period_start,
        "period_end": record.period_end,
        "timezone": record.timezone,
        "status": record.status.value,
        "version": record.version,
        "supersedes_review_id": record.supersedes_review_id,
        "input_fingerprint": record.input_fingerprint,
        "model_id": record.model_id,
        "generation_attempts": record.generation_attempts,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "completed_at": record.completed_at,
        "error": record.error,
    }
    if record.review_type is ReviewType.WEEKLY:
        payload["week_id"] = (
            record.summary.week_id
            if isinstance(record.summary, WeeklyReview)
            else iso_week_id(record.date)
        )
    if record.summary is not None:
        payload["overview"] = record.summary.overview
    return payload


def _detail_payload(
    record: StoredReview,
    delivery: StoredReviewDelivery | None = None,
) -> dict[str, object]:
    return {
        **_summary_payload(record),
        "delivery": delivery_payload(delivery),
        "source_manifest": [
            entry.model_dump(mode="json") for entry in record.source_manifest
        ],
        "collection_status": [
            status.model_dump(mode="json") for status in record.collection_status
        ],
        "source_snapshots": [
            source.model_dump(mode="json") for source in record.source_snapshots
        ],
        "summary": (
            None if record.summary is None else record.summary.model_dump(mode="json")
        ),
        "summary_markdown": record.summary_markdown,
    }


def _integer(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _failed_schedule_entries(
    coordinator: object | None, period_field: str
) -> list[dict[str, object]] | None:
    """Expose only the failed periods an operator may retry."""
    if coordinator is None:
        return None
    state = getattr(coordinator, "state", None)
    if not isinstance(state, dict):
        raise ValueError("review schedule state is unavailable")
    key = "failed_days" if period_field == "day" else "failed_weeks"
    raw = state.get(key)
    if not isinstance(raw, dict):
        return []
    entries: list[dict[str, object]] = []
    for period, detail in raw.items():
        if not isinstance(period, str) or not isinstance(detail, dict):
            continue
        review_id = detail.get("review_id")
        error = detail.get("error")
        at = detail.get("at")
        entries.append(
            {
                period_field: period,
                "review_id": review_id if isinstance(review_id, str) else "",
                "error": (
                    error if isinstance(error, str) and error else "generation_failed"
                ),
                "at": at if isinstance(at, str) else "",
            }
        )
    entries.sort(key=lambda entry: str(entry[period_field]), reverse=True)
    return entries[:100]


def review_routes(
    service: DailyReviewService,
    runtime_token: str | None = None,
    weekly: WeeklyReviewService | None = None,
    *,
    daily_cutoff: time | None = None,
    host_timezone: str | None = None,
) -> list[BaseRoute]:
    async def create_daily(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = DailyReviewRequest.model_validate(await _body(request))
            # The host schedule keys a period by its cutoff. Manual generation
            # in the host timezone must use the same window or it would create
            # a parallel version instead of superseding the scheduled report.
            cutoff = (
                daily_cutoff
                if daily_cutoff is not None and payload.timezone == host_timezone
                else None
            )
            record = await service.generate_daily_review(
                payload.date,
                payload.request_id,
                timezone=payload.timezone,
                regenerate=payload.regenerate,
                cutoff=cutoff,
            )
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except ValidationError:
            return _error(422, "invalid_review_request")
        except ReviewRequestConflict:
            return _error(409, "request_id_conflict")
        except ReviewPeriodCompleted:
            return _error(409, "review_period_completed")
        except ReviewNoContent:
            return _error(409, "review_no_content")
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        return JSONResponse(
            _summary_payload(record), status_code=202, headers=_json_headers()
        )

    async def create_weekly(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        if weekly is None:
            return _error(503, "weekly_review_unavailable")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = WeeklyReviewRequest.model_validate(await _body(request))
            record = await weekly.generate_weekly_review(
                payload.week_id,
                payload.request_id,
                timezone=payload.timezone,
                regenerate=payload.regenerate,
            )
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except ValidationError:
            return _error(422, "invalid_review_request")
        except ReviewRequestConflict:
            return _error(409, "request_id_conflict")
        except ReviewPeriodNotEnded:
            return _error(409, "review_period_not_ended")
        except ReviewPeriodCompleted:
            return _error(409, "review_period_completed")
        except ReviewNoContent:
            return _error(409, "review_no_content")
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        return JSONResponse(
            _summary_payload(record), status_code=202, headers=_json_headers()
        )

    async def listing(request: Request) -> JSONResponse:
        try:
            raw_type = request.query_params.get("type")
            review_type = None if not raw_type else ReviewType(raw_type)
            raw_day = request.query_params.get("day")
            raw_week = request.query_params.get("week_id")
            if raw_day and raw_week:
                raise ValueError("use either day or week_id, not both")
            day = None
            if raw_day:
                day = date.fromisoformat(raw_day)
            elif raw_week:
                # Weekly records are keyed by the Sunday that ends the window.
                day = week_period(raw_week.strip(), timezone="UTC").day
            records = await service.list_reviews(
                review_type=review_type,
                day=day,
                limit=_integer(request, "limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT),
                offset=_integer(request, "offset", 0, 0, 1_000_000),
            )
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        return JSONResponse(
            {"reviews": [_summary_payload(record) for record in records]},
            headers=_json_headers(),
        )

    async def detail(request: Request) -> JSONResponse:
        review_id = request.path_params.get("review_id", "")
        try:
            record = await service.get_review(review_id)
            delivery = await asyncio.to_thread(service.store.get_delivery, review_id)
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        if record is None:
            return _error(404, "review_not_found")
        return JSONResponse(_detail_payload(record, delivery), headers=_json_headers())

    async def schedule_status(request: Request) -> JSONResponse:
        try:
            daily = _failed_schedule_entries(
                getattr(request.app.state, "review_schedule", None), "day"
            )
            weekly = _failed_schedule_entries(
                getattr(request.app.state, "weekly_review_schedule", None), "week_id"
            )
        except (ValueError, OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        return JSONResponse({"daily": daily, "weekly": weekly}, headers=_json_headers())

    async def retry_schedule(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = ScheduleRetryRequest.model_validate(await _body(request))
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except ValidationError:
            return _error(422, "invalid_review_request")
        coordinator = getattr(request.app.state, "review_schedule", None)
        if coordinator is None:
            return _error(503, "review_schedule_disabled")
        try:
            record = await coordinator.retry_failed(payload.day, payload.request_id)
        except ReviewNoContent:
            return _error(409, "review_no_content")
        except RuntimeError:
            return _error(503, "review_schedule_busy")
        except ValueError:
            return _error(409, "retry_request_conflict")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        if record is None:
            return _error(409, "review_day_not_failed")
        return JSONResponse(
            _summary_payload(record), status_code=202, headers=_json_headers()
        )

    async def retry_weekly_schedule(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = WeeklyScheduleRetryRequest.model_validate(await _body(request))
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except ValidationError:
            return _error(422, "invalid_review_request")
        coordinator = getattr(request.app.state, "weekly_review_schedule", None)
        if coordinator is None:
            return _error(503, "weekly_review_schedule_disabled")
        try:
            record = await coordinator.retry_failed(
                payload.week_id.strip(), payload.request_id
            )
        except ReviewNoContent:
            return _error(409, "review_no_content")
        except RuntimeError:
            return _error(503, "review_schedule_busy")
        except ValueError:
            return _error(409, "retry_request_conflict")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        if record is None:
            return _error(409, "review_week_not_failed")
        return JSONResponse(
            _summary_payload(record), status_code=202, headers=_json_headers()
        )

    async def resend(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        review_id = request.path_params.get("review_id", "")
        try:
            raw = await _read_limited(request)
        except ValueError:
            return _error(413, "review_request_too_large")
        try:
            payload = DeliveryResendRequest.model_validate(json.loads(raw))
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except ValidationError:
            return _error(422, "invalid_review_request")
        try:
            current = await asyncio.to_thread(service.store.get_delivery, review_id)
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        if (
            current is not None
            and current.status is ReviewDeliveryStatus.DELIVERY_UNKNOWN
            and not payload.confirmed
        ):
            return _error(409, "review_delivery_confirmation_required")
        coordinator = getattr(request.app.state, "review_delivery", None)
        target = getattr(coordinator, "target", None) if payload.rebind else None
        if payload.rebind and target is None:
            return _error(409, "review_delivery_target_unavailable")
        try:
            result = await asyncio.to_thread(
                service.store.resend_delivery,
                review_id,
                request_id=payload.request_id,
                rebind=payload.rebind,
                target=target,
            )
        except ReviewRequestConflict:
            return _error(409, "request_id_conflict")
        except KeyError:
            return _error(404, "review_delivery_not_found")
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, PostgresError):
            return _error(503, "review_store_unavailable")
        if not result.accepted:
            return _error(409, result.error or "review_delivery_not_retryable")
        if coordinator is not None:
            coordinator.rescan(force=True)
        return JSONResponse(
            {
                "delivery": delivery_payload(result.delivery),
                "request_id": result.request_id,
                "replayed": result.replayed,
            },
            status_code=202,
            headers=_json_headers(),
        )

    return [
        Route(
            "/api/knowledge/reviews/daily",
            create_daily,
            methods=["POST"],
        ),
        Route(
            "/api/knowledge/reviews/weekly",
            create_weekly,
            methods=["POST"],
        ),
        Route("/api/knowledge/reviews", listing, methods=["GET"]),
        Route(
            "/api/knowledge/reviews/schedule",
            schedule_status,
            methods=["GET"],
        ),
        Route(
            "/api/knowledge/reviews/schedule/retry",
            retry_schedule,
            methods=["POST"],
        ),
        Route(
            "/api/knowledge/reviews/schedule/weekly/retry",
            retry_weekly_schedule,
            methods=["POST"],
        ),
        Route(
            "/api/knowledge/reviews/{review_id}",
            detail,
            methods=["GET"],
        ),
        Route(
            "/api/knowledge/reviews/{review_id}/delivery/resend",
            resend,
            methods=["POST"],
        ),
    ]


__all__ = ["DailyReviewRequest", "review_routes"]
