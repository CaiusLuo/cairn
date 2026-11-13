"""Durable review identities, collected source records and model output.

Review data is derived from existing Clips, Tasks/Runs and the personal daily
journal.  It never becomes an authority over those records: the source manifest
keeps stable identities and hashes so a report can always be audited against
what was actually available.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

REVIEW_REQUEST_ID_MAX_LENGTH = 200
REVIEW_TIMEZONE_MAX_LENGTH = 64
MAX_SOURCE_REFS = 128
MAX_ITEM_SOURCE_REFS = 16


class ReviewType(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceTrust(StrEnum):
    """How much authority a source has for factual claims."""

    USER_PROVIDED = "user_provided"
    USER_RECORDED = "user_recorded"
    SYSTEM_RECEIPT = "system_receipt"
    AGENT_INFERRED = "agent_inferred"


class SourceCollectionState(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


def new_review_id() -> str:
    return "review-" + uuid4().hex


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ``ReviewInput.date`` shadows the imported type inside the class body.
Date = date


class SourceManifestEntry(BaseModel):
    """One source identity retained independently of its prompt snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: str = Field(min_length=1, max_length=80)
    trust: SourceTrust
    may_prove_completion: bool = False
    verification: str = Field(default="", max_length=80)
    included: bool = True
    omitted_reason: str | None = Field(default=None, max_length=80)
    truncated: bool = False


class SourceCollectionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=40)
    status: SourceCollectionState
    discovered: int = Field(default=0, ge=0)
    included: int = Field(default=0, ge=0)
    omitted: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("notes")
    @classmethod
    def _notes_are_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if len(value) > 300:
                raise ValueError("a collection note must be at most 300 characters")
        return values


class SourceSnapshot(BaseModel):
    """A source plus the bounded text actually offered to the review model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: str = Field(min_length=1, max_length=80)
    trust: SourceTrust
    may_prove_completion: bool = False
    verification: str = Field(default="", max_length=80)
    snapshot: str = Field(default="", max_length=100_000)
    truncated: bool = False
    included: bool = True
    omitted_reason: str | None = Field(default=None, max_length=80)

    def manifest_entry(self) -> SourceManifestEntry:
        return SourceManifestEntry(
            source_type=self.source_type,
            source_id=self.source_id,
            content_hash=self.content_hash,
            occurred_at=self.occurred_at,
            trust=self.trust,
            may_prove_completion=self.may_prove_completion,
            verification=self.verification,
            included=self.included,
            omitted_reason=self.omitted_reason,
            truncated=self.truncated,
        )


def review_input_fingerprint(
    *,
    review_type: ReviewType,
    day: date,
    timezone: str,
    period_start: datetime,
    period_end: datetime,
    sources: tuple[SourceSnapshot, ...],
    collection_status: tuple[SourceCollectionStatus, ...],
) -> str:
    """Hash the deterministic review input, including omissions and trust."""
    payload = {
        "review_type": review_type.value,
        "date": day.isoformat(),
        "timezone": timezone,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "sources": [source.model_dump(mode="json") for source in sources],
        "collection_status": [
            status.model_dump(mode="json") for status in collection_status
        ],
    }
    return _sha256(_json(payload))


class ReviewInput(BaseModel):
    """Bounded, auditable input for one review period."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_type: ReviewType
    date: date
    timezone: str = Field(min_length=1, max_length=REVIEW_TIMEZONE_MAX_LENGTH)
    period_start: datetime
    period_end: datetime
    sources: tuple[SourceSnapshot, ...]
    collection_status: tuple[SourceCollectionStatus, ...]
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_chars: int = Field(ge=1)
    used_chars: int = Field(ge=0)

    @classmethod
    def build(
        cls,
        *,
        review_type: ReviewType,
        day: Date,
        timezone: str,
        period_start: datetime,
        period_end: datetime,
        sources: tuple[SourceSnapshot, ...],
        collection_status: tuple[SourceCollectionStatus, ...],
        budget_chars: int,
    ) -> ReviewInput:
        fingerprint = review_input_fingerprint(
            review_type=review_type,
            day=day,
            timezone=timezone,
            period_start=period_start,
            period_end=period_end,
            sources=sources,
            collection_status=collection_status,
        )
        return cls(
            review_type=review_type,
            date=day,
            timezone=timezone,
            period_start=period_start,
            period_end=period_end,
            sources=sources,
            collection_status=collection_status,
            input_fingerprint=fingerprint,
            budget_chars=budget_chars,
            used_chars=sum(
                len(source.snapshot) for source in sources if source.included
            ),
        )

    @property
    def included_sources(self) -> tuple[SourceSnapshot, ...]:
        return tuple(source for source in self.sources if source.included)

    @property
    def completion_source_ids(self) -> frozenset[str]:
        return frozenset(
            source.source_id
            for source in self.included_sources
            if source.may_prove_completion
        )

    @property
    def included_source_ids(self) -> frozenset[str]:
        return frozenset(source.source_id for source in self.included_sources)


class ReviewItem(BaseModel):
    """One conclusion and the exact source identities supporting it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    source_refs: tuple[str, ...] = Field(default=(), max_length=MAX_ITEM_SOURCE_REFS)

    @field_validator("text")
    @classmethod
    def _text_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review item text must not be blank")
        return value

    @field_validator("source_refs")
    @classmethod
    def _refs_are_clean(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("source refs must be strings")
            item = value.strip().strip("`")
            if not item:
                raise ValueError("source refs must not be blank")
            if len(item) > 200:
                raise ValueError("a source ref must be at most 200 characters")
            if item not in cleaned:
                cleaned.append(item)
        return tuple(cleaned)


class DailyReview(BaseModel):
    """Server-validated structured report; it is data, never an instruction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    overview: str = Field(min_length=1, max_length=4000)
    completed_activities: tuple[ReviewItem, ...] = Field(default=(), max_length=5)
    knowledge_insights: tuple[ReviewItem, ...] = Field(default=(), max_length=3)
    problems: tuple[ReviewItem, ...] = Field(default=(), max_length=3)
    recommendations: tuple[ReviewItem, ...] = Field(default=(), max_length=3)
    source_refs: tuple[str, ...] = Field(default=(), max_length=MAX_SOURCE_REFS)
    confidence_notes: tuple[str, ...] = Field(default=(), max_length=5)

    @field_validator("overview")
    @classmethod
    def _overview_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("overview must not be blank")
        return value

    @field_validator("source_refs")
    @classmethod
    def _top_refs_are_clean(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("source refs must be strings")
            item = value.strip().strip("`")
            if not item:
                raise ValueError("source refs must not be blank")
            if len(item) > 200:
                raise ValueError("a source ref must be at most 200 characters")
            if item not in cleaned:
                cleaned.append(item)
        return tuple(cleaned)

    @field_validator("confidence_notes")
    @classmethod
    def _confidence_notes_are_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not value.strip():
                raise ValueError("confidence notes must not be blank")
            if len(value) > 500:
                raise ValueError("a confidence note must be at most 500 characters")
        return values


class StoredReview(BaseModel):
    """One immutable review version as persisted in the knowledge database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str = Field(min_length=1, max_length=100)
    review_type: ReviewType
    date: date
    period_start: str = Field(min_length=1, max_length=80)
    period_end: str = Field(min_length=1, max_length=80)
    timezone: str = Field(min_length=1, max_length=REVIEW_TIMEZONE_MAX_LENGTH)
    status: ReviewStatus
    source_manifest: tuple[SourceManifestEntry, ...]
    collection_status: tuple[SourceCollectionStatus, ...]
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: DailyReview | None = None
    summary_markdown: str | None = None
    model_id: str | None = None
    generation_attempts: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    completed_at: str | None = None
    error: str | None = None
    version: int = Field(default=1, ge=1)
    supersedes_review_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewCreateResult:
    review: StoredReview
    created: bool
    reason: Literal[
        "created",
        "request_replay",
        "active_reused",
        "completed_reused",
    ]


__all__ = [
    "MAX_ITEM_SOURCE_REFS",
    "MAX_SOURCE_REFS",
    "REVIEW_REQUEST_ID_MAX_LENGTH",
    "REVIEW_TIMEZONE_MAX_LENGTH",
    "DailyReview",
    "ReviewCreateResult",
    "ReviewInput",
    "ReviewItem",
    "ReviewStatus",
    "ReviewType",
    "SourceCollectionState",
    "SourceCollectionStatus",
    "SourceManifestEntry",
    "SourceSnapshot",
    "SourceTrust",
    "StoredReview",
    "new_review_id",
    "review_input_fingerprint",
]
