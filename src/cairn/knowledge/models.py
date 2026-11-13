"""Clip identities and validation shared by the store and HTTP surface.

A Clip is user-owned reference text. It is deliberately not a Conversation,
Memory entry or Agent prompt fragment: reading it back requires the Web/API
surface, never an implicit turn context.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CLIP_CONTENT_MAX_LENGTH = 100_000
CLIP_SOURCE_MAX_LENGTH = 40
CLIP_TAG_MAX_LENGTH = 64
CLIP_TAGS_MAX_COUNT = 32
CLIP_REQUEST_ID_MAX_LENGTH = 200


def new_clip_id() -> str:
    return "clip-" + uuid4().hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def content_hash(content: str) -> str:
    """Return the stable digest of the exact stored text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_tags(tags: object) -> tuple[str, ...]:
    if isinstance(tags, str) or not isinstance(tags, (list, tuple)):
        raise ValueError("tags must be a list of strings")
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("each tag must be a string")
        value = tag.strip()
        if not value:
            raise ValueError("tags must not be blank")
        if len(value) > CLIP_TAG_MAX_LENGTH:
            raise ValueError(f"a tag must be at most {CLIP_TAG_MAX_LENGTH} characters")
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > CLIP_TAGS_MAX_COUNT:
        raise ValueError(f"at most {CLIP_TAGS_MAX_COUNT} tags are allowed")
    return tuple(normalized)


def normalize_source(source: str) -> str:
    value = source.strip()
    if not value:
        raise ValueError("source must not be blank")
    if len(value) > CLIP_SOURCE_MAX_LENGTH:
        raise ValueError(f"source must be at most {CLIP_SOURCE_MAX_LENGTH} characters")
    return value


class Clip(BaseModel):
    """One durable knowledge fragment; ``content`` is preserved verbatim."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    clip_id: str = Field(default_factory=new_clip_id, min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=CLIP_CONTENT_MAX_LENGTH)
    source: str = Field(default="web", max_length=CLIP_SOURCE_MAX_LENGTH)
    tags: tuple[str, ...] = ()
    # SHA-256 of the exact text above. The store, not a client, owns this.
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    archived_at: str | None = None

    @field_validator("content")
    @classmethod
    def _content_has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("source")
    @classmethod
    def _source_has_text(cls, value: str) -> str:
        return normalize_source(value)

    @field_validator("tags")
    @classmethod
    def _tags_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_tags(value)

    @model_validator(mode="before")
    @classmethod
    def _fill_content_hash(cls, data: object) -> object:
        if (
            isinstance(data, dict)
            and "content_hash" not in data
            and isinstance(data.get("content"), str)
        ):
            return {**data, "content_hash": content_hash(data["content"])}
        return data

    @model_validator(mode="after")
    def _content_hash_matches(self) -> Clip:
        if self.content_hash != content_hash(self.content):
            raise ValueError("content_hash does not match content")
        return self
