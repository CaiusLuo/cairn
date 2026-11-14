"""SQLite authority for private knowledge clips.

The database lives in a host-selected directory outside the Agent workspace.
Nothing here is read by an Agent turn, Memory store or prompt builder; callers
must go through this store's explicit API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from cairn.knowledge.models import (
    CLIP_CONTENT_MAX_LENGTH,
    CLIP_REQUEST_ID_MAX_LENGTH,
    Clip,
    content_hash,
    new_clip_id,
    normalize_source,
    normalize_tags,
    utc_now,
)
from cairn.knowledge.schema import SCHEMA_VERSION, create_schema

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


class KnowledgeRequestConflict(RuntimeError):
    """A request_id was reused for a different create payload."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class KnowledgeStore:
    """One private SQLite file; every mutation is local and transactional."""

    def __init__(self, root: Path) -> None:
        raw = root.expanduser()
        if raw.is_symlink():
            raise ValueError("Knowledge directory cannot be a symlink")
        self.root = raw.resolve()
        if self.root.is_symlink():
            raise ValueError("Knowledge directory cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self.root.chmod(0o700)
        self.path = self.root / "knowledge.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Knowledge store cannot be a symlink")
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            # Older Clip/Review schemas are upgraded additively by
            # ``create_schema``; no export or rebuild is required.
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise ValueError("Unsupported knowledge store schema")
            self._create_schema(connection)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        with suppress(OSError):
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        create_schema(connection)

    @staticmethod
    def _clip(row: sqlite3.Row) -> Clip:
        tags = json.loads(cast(str, row["tags_json"]))
        if not isinstance(tags, list):
            raise ValueError("knowledge store contains invalid tags")
        return Clip(
            clip_id=cast(str, row["clip_id"]),
            content=cast(str, row["content"]),
            source=cast(str, row["source"]),
            tags=tuple(cast(list[str], tags)),
            content_hash=cast(str, row["content_hash"]),
            created_at=cast(str, row["created_at"]),
            updated_at=cast(str, row["updated_at"]),
            archived_at=cast(str | None, row["archived_at"]),
        )

    def create(
        self,
        content: str,
        *,
        source: str = "web",
        tags: object = (),
        request_id: str | None = None,
    ) -> tuple[Clip, bool]:
        """Create a clip once per request_id; return (clip, was_created)."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must not be blank")
        if len(content) > CLIP_CONTENT_MAX_LENGTH:
            raise ValueError(
                f"content must be at most {CLIP_CONTENT_MAX_LENGTH} characters"
            )
        normalized_source = normalize_source(source)
        normalized_tags = normalize_tags(tags)
        if request_id is None:
            request_key = "auto-" + uuid4().hex
        else:
            request_key = request_id.strip()
            if not request_key:
                raise ValueError("request_id must not be blank")
            if len(request_key) > CLIP_REQUEST_ID_MAX_LENGTH:
                raise ValueError(
                    f"request_id must be at most {CLIP_REQUEST_ID_MAX_LENGTH} characters"
                )
        request_fingerprint = hashlib.sha256(
            _json(
                {
                    "content": content,
                    "source": normalized_source,
                    "tags": normalized_tags,
                }
            ).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT fingerprint, clip_id FROM clip_requests WHERE request_id = ?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != request_fingerprint:
                    raise KnowledgeRequestConflict(
                        "request_id was already used for different content"
                    )
                row = connection.execute(
                    "SELECT * FROM clips WHERE clip_id = ?",
                    (existing["clip_id"],),
                ).fetchone()
                if row is None:
                    raise ValueError("knowledge request points at a missing clip")
                return self._clip(row), False
            clip = Clip(
                clip_id=new_clip_id(),
                content=content,
                source=normalized_source,
                tags=normalized_tags,
                content_hash=content_hash(content),
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                "INSERT INTO clips (clip_id, content, source, tags_json, "
                "content_hash, created_at, updated_at, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    clip.clip_id,
                    clip.content,
                    clip.source,
                    _json(list(clip.tags)),
                    clip.content_hash,
                    clip.created_at,
                    clip.updated_at,
                ),
            )
            connection.execute(
                "INSERT INTO clip_requests (request_id, fingerprint, clip_id) "
                "VALUES (?, ?, ?)",
                (request_key, request_fingerprint, clip.clip_id),
            )
            return clip, True

    def get(self, clip_id: str) -> Clip | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM clips WHERE clip_id = ?", (clip_id,)
            ).fetchone()
        return None if row is None else self._clip(row)

    def list_clips(
        self,
        *,
        query: str | None = None,
        include_archived: bool = False,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[Clip, ...]:
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        where: list[str] = []
        params: list[object] = []
        if not include_archived:
            where.append("archived_at IS NULL")
        needle = (query or "").strip()
        if needle:
            if len(needle) > 500:
                raise ValueError("query must be at most 500 characters")
            where.append(
                "(instr(lower(content), lower(?)) > 0 "
                "OR instr(lower(tags_json), lower(?)) > 0)"
            )
            params.extend((needle, needle))
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clips"
                + clause
                + " ORDER BY updated_at DESC, clip_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return tuple(self._clip(row) for row in rows)

    def list_clips_touched_between(
        self,
        start: str,
        end: str,
        *,
        include_archived: bool = False,
    ) -> tuple[Clip, ...]:
        """Return clips whose exact stored timestamp falls in ``[start, end)``.

        ``start`` and ``end`` are canonical UTC ISO strings. The window is
        intentionally based on ``updated_at``: a clip created that day and a
        clip edited that day are both included, while an untouched older clip is
        not. Archived clips are excluded by default; callers that explicitly
        need them can opt in.
        """
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("review window bounds must be strings")
        if not start or not end or start >= end:
            raise ValueError("review window is invalid")
        if type(include_archived) is not bool:
            raise ValueError("include_archived must be a boolean")
        archived_filter = "" if include_archived else "AND archived_at IS NULL "
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clips WHERE updated_at >= ? AND updated_at < ? "
                + archived_filter
                + "ORDER BY updated_at, clip_id",
                (start, end),
            ).fetchall()
        return tuple(self._clip(row) for row in rows)

    def update(
        self,
        clip_id: str,
        *,
        content: str | None = None,
        source: str | None = None,
        tags: object | None = None,
    ) -> Clip:
        if content is None and source is None and tags is None:
            raise ValueError("an update needs content, source or tags")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM clips WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            if row is None:
                raise KeyError(clip_id)
            current = self._clip(row)
            updated = Clip(
                clip_id=current.clip_id,
                content=current.content if content is None else content,
                source=current.source if source is None else source,
                tags=current.tags if tags is None else normalize_tags(tags),
                content_hash=content_hash(
                    current.content if content is None else content
                ),
                created_at=current.created_at,
                updated_at=utc_now(),
                archived_at=current.archived_at,
            )
            connection.execute(
                "UPDATE clips SET content = ?, source = ?, tags_json = ?, "
                "content_hash = ?, updated_at = ? WHERE clip_id = ?",
                (
                    updated.content,
                    updated.source,
                    _json(list(updated.tags)),
                    updated.content_hash,
                    updated.updated_at,
                    updated.clip_id,
                ),
            )
            return updated

    def set_archived(self, clip_id: str, *, archived: bool) -> Clip:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM clips WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            if row is None:
                raise KeyError(clip_id)
            current = self._clip(row)
            if (current.archived_at is not None) == archived:
                return current
            updated = current.model_copy(
                update={
                    "archived_at": utc_now() if archived else None,
                    "updated_at": utc_now(),
                }
            )
            connection.execute(
                "UPDATE clips SET archived_at = ?, updated_at = ? WHERE clip_id = ?",
                (updated.archived_at, updated.updated_at, updated.clip_id),
            )
            return updated


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "KnowledgeRequestConflict",
    "KnowledgeStore",
]
