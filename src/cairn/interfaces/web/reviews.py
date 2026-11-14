"""Authenticated review history and non-blocking daily generation API."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.interfaces.web.auth import operator_request
from cairn.review.models import (
    REVIEW_REQUEST_ID_MAX_LENGTH,
    REVIEW_TIMEZONE_MAX_LENGTH,
    ReviewType,
    StoredReview,
)
from cairn.review.service import DailyReviewService
from cairn.review.store import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ReviewPeriodCompleted,
    ReviewRequestConflict,
)

MAX_BODY_BYTES = 16 * 1024


def _json_headers() -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_json_headers())


async def _body(request: Request) -> object:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ValueError("review request is too large")
        body.extend(chunk)
    return json.loads(body)


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
    if record.summary is not None:
        payload["overview"] = record.summary.overview
    return payload


def _detail_payload(record: StoredReview) -> dict[str, object]:
    return {
        **_summary_payload(record),
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


def review_routes(
    service: DailyReviewService, runtime_token: str | None = None
) -> list[BaseRoute]:
    async def create_daily(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = DailyReviewRequest.model_validate(await _body(request))
            record = await service.generate_daily_review(
                payload.date,
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
        except ReviewPeriodCompleted:
            return _error(409, "review_period_completed")
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, sqlite3.Error):
            return _error(503, "review_store_unavailable")
        return JSONResponse(
            _summary_payload(record), status_code=202, headers=_json_headers()
        )

    async def listing(request: Request) -> JSONResponse:
        try:
            raw_type = request.query_params.get("type")
            review_type = None if not raw_type else ReviewType(raw_type)
            records = await service.list_reviews(
                review_type=review_type,
                limit=_integer(request, "limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT),
                offset=_integer(request, "offset", 0, 0, 1_000_000),
            )
        except ValueError:
            return _error(422, "invalid_review_request")
        except (OSError, sqlite3.Error):
            return _error(503, "review_store_unavailable")
        return JSONResponse(
            {"reviews": [_summary_payload(record) for record in records]},
            headers=_json_headers(),
        )

    async def detail(request: Request) -> JSONResponse:
        review_id = request.path_params.get("review_id", "")
        try:
            record = await service.get_review(review_id)
        except (OSError, sqlite3.Error):
            return _error(503, "review_store_unavailable")
        if record is None:
            return _error(404, "review_not_found")
        return JSONResponse(_detail_payload(record), headers=_json_headers())

    return [
        Route(
            "/api/knowledge/reviews/daily",
            create_daily,
            methods=["POST"],
        ),
        Route("/api/knowledge/reviews", listing, methods=["GET"]),
        Route(
            "/api/knowledge/reviews/{review_id}",
            detail,
            methods=["GET"],
        ),
    ]


__all__ = ["DailyReviewRequest", "review_routes"]
