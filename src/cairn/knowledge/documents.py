"""Document identities, ingestion states and deterministic chunking.

A Document is one file the owner added to the private library. It becomes
searchable only after extraction and chunking succeed; embedding and graph
status are tracked separately so either can fail without losing retrieval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_KIND_UPLOAD = "upload"

DOCUMENT_PENDING = "pending"
DOCUMENT_PROCESSING = "processing"
DOCUMENT_READY = "ready"
DOCUMENT_FAILED = "failed"

STEP_PENDING = "pending"
STEP_READY = "ready"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
MAX_CHUNKS = 400

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Prefer prose boundaries, then line breaks; a window always cuts somewhere.
_BOUNDARIES = ("\n\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "\n")  # noqa: RUF001


def new_document_id() -> str:
    return "doc-" + uuid4().hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    char_start: int
    char_end: int
    token_count: int
    heading_path: tuple[str, ...] | None = None


class Document(BaseModel):
    """One stored upload and its ingestion state."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_id: str = Field(
        default_factory=new_document_id, min_length=1, max_length=100
    )
    owner: str = Field(min_length=1, max_length=100)
    kind: str = Field(default=DOCUMENT_KIND_UPLOAD, min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    media_type: str = Field(min_length=1, max_length=200)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    status: str = DOCUMENT_PENDING
    embedding_status: str = STEP_PENDING
    graph_status: str = STEP_PENDING
    error: str | None = None
    attempts: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    processed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """A cheap ceiling-ish estimate: CJK characters plus grouped Latin words."""
    cjk = len(_CJK.findall(text))
    words = len(_WORD.findall(text))
    return max(1, cjk + (words * 4 + 2) // 3)


def chunk_text(
    text: str,
    *,
    chunk_chars: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
    max_chunks: int = MAX_CHUNKS,
    markdown: bool = False,
) -> tuple[TextChunk, ...]:
    """Split extracted text into overlapping, citeable chunks.

    Offsets are character positions in ``text``. Chunks prefer paragraph and
    sentence boundaries and never include leading/trailing whitespace.
    """
    if chunk_chars < 64:
        raise ValueError("chunk_chars must be at least 64")
    if not 0 <= overlap < chunk_chars:
        raise ValueError("overlap must be between 0 and chunk_chars")
    if max_chunks < 1:
        raise ValueError("max_chunks must be positive")
    spans = _heading_spans(text) if markdown else ()
    chunks: list[TextChunk] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < max_chunks:
        end = min(start + chunk_chars, length)
        if end < length:
            end = _cut_point(text, start, end)
        raw = text[start:end]
        chunk_start = start + len(raw) - len(raw.lstrip())
        chunk_end = end - (len(raw) - len(raw.rstrip()))
        if chunk_end > chunk_start:
            body = text[chunk_start:chunk_end]
            chunks.append(
                TextChunk(
                    text=body,
                    char_start=chunk_start,
                    char_end=chunk_end,
                    token_count=estimate_tokens(body),
                    heading_path=_heading_path(spans, chunk_start),
                )
            )
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return tuple(chunks)


def _cut_point(text: str, start: int, limit: int) -> int:
    """Prefer the last natural boundary in the second half of the window."""
    floor = start + (limit - start) // 2
    window = text[start:limit]
    best = -1
    for marker in _BOUNDARIES:
        index = window.rfind(marker)
        if index >= 0:
            candidate = start + index + len(marker)
            if candidate >= floor:
                best = max(best, candidate)
    return best if best > start else limit


def _heading_spans(text: str) -> tuple[tuple[int, int, str], ...]:
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _HEADING.match(line.rstrip("\r\n"))
        if match:
            spans.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)
    return tuple(spans)


def _heading_path(
    spans: tuple[tuple[int, int, str], ...], position: int
) -> tuple[str, ...] | None:
    stack: list[tuple[int, str]] = []
    for start, level, title in spans:
        if start > position:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return tuple(title for _, title in stack) or None


__all__ = [
    "CHUNK_CHARS",
    "CHUNK_OVERLAP",
    "DOCUMENT_FAILED",
    "DOCUMENT_KIND_UPLOAD",
    "DOCUMENT_PENDING",
    "DOCUMENT_PROCESSING",
    "DOCUMENT_READY",
    "MAX_CHUNKS",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_READY",
    "STEP_SKIPPED",
    "Document",
    "TextChunk",
    "chunk_text",
    "estimate_tokens",
    "new_document_id",
    "utc_now",
]
