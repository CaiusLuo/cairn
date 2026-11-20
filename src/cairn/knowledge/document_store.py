"""PostgreSQL and disk authority for the private document library.

Originals live outside the Agent workspace as 0600 files named by content hash;
the database row is the authority for state, deduplication and search. A single
ingestion worker (the Server process) claims rows, so claims use
``FOR UPDATE SKIP LOCKED`` and a restart requeues interrupted work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from cairn.conversation.attachments import safe_attachment_name
from cairn.knowledge.documents import (
    DOCUMENT_FAILED,
    DOCUMENT_KIND_UPLOAD,
    DOCUMENT_PENDING,
    DOCUMENT_PROCESSING,
    DOCUMENT_READY,
    MAX_DOCUMENT_TEXT_CHARS,
    STEP_PENDING,
    Document,
    TextChunk,
)
from cairn.shared.artifacts import atomic_write_private_bytes
from cairn.shared.db import Database, DbConnection, Row
from cairn.shared.text_extract import is_supported

DEFAULT_OWNER = "owner"
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Terminal statuses record when ingestion finished; pending/processing do not.
_TERMINAL = frozenset({DOCUMENT_READY, DOCUMENT_FAILED})
_ORIGINAL_NAME = re.compile(
    r"[0-9a-f]{64}(?:-doc-[0-9a-f]{32}|\.[^/]+)?(?:\.tmp-[0-9a-f]{32})?"
)


class EmbeddingSpaceMismatch(RuntimeError):
    """The configured model returned a different dimension than before."""


@dataclass(frozen=True, slots=True)
class DocumentTextPage:
    document: Document
    content: str
    parser: str
    parser_version: str
    offset: int
    total_chars: int

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.content)
        return end if end < self.total_chars else None


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    chunk_id: int
    text: str
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class MissingChunk:
    chunk_id: int
    text: str


@dataclass(frozen=True, slots=True)
class EntityRecord:
    name: str
    kind: str
    chunk_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RelationRecord:
    source: str
    target: str
    relation: str
    chunk_id: int | None


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _optional_iso(value: object) -> str | None:
    return None if value is None else _iso(value)


def _document(row: Row) -> Document:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return Document(
        document_id=cast(str, row["document_id"]),
        owner=cast(str, row["owner"]),
        kind=cast(str, row["kind"]),
        title=cast(str, row["title"]),
        media_type=cast(str, row["media_type"]),
        sha256=cast(str, row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        status=cast(str, row["status"]),
        embedding_status=cast(str, row["embedding_status"]),
        graph_status=cast(str, row["graph_status"]),
        error=cast(str | None, row["error"]),
        attempts=int(row["attempts"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        processed_at=_optional_iso(row["processed_at"]),
        metadata=cast(dict[str, object], metadata or {}),
    )


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def _normalized(name: str) -> str:
    return " ".join(name.casefold().split())


class DocumentStore:
    """The owner's durable document library and its ingestion queue."""

    def __init__(
        self,
        database: Database,
        root: Path,
        *,
        owner: str = DEFAULT_OWNER,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        if not owner.strip():
            raise ValueError("document owner must not be blank")
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        self.db = database
        self.owner = owner
        self.max_upload_bytes = max_upload_bytes
        root = root.expanduser()
        if root.is_symlink():
            raise ValueError("document originals directory cannot be a symlink")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    @contextmanager
    def _connect(self) -> Iterator[DbConnection]:
        with self.db.connect("knowledge") as connection:
            yield connection

    def create_upload(
        self,
        title: str,
        media_type: str,
        data: bytes,
        *,
        source_url: str | None = None,
    ) -> tuple[Document, bool]:
        """Store one upload once per ``(owner, sha256)``; the flag says created."""
        if not data:
            raise ValueError("Document is empty")
        if len(data) > self.max_upload_bytes:
            raise ValueError("Document exceeds the upload limit")
        name = safe_attachment_name(title or "document")
        media = (media_type or "application/octet-stream").split(";")[0].strip()
        media = media.lower() or "application/octet-stream"
        if not is_supported(name, media):
            raise ValueError("Unsupported document type")
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as connection:
            # Keep startup cleanup out of the interval before the row commits.
            connection.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
                (f"document-originals:{self.root}",),
            )
            # Serialize duplicate uploads of the same bytes before deciding
            # which row (and display title) wins.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"document-upload:{self.owner}:{digest}",),
            )
            existing = connection.execute(
                "SELECT * FROM documents WHERE owner = %s AND sha256 = %s FOR UPDATE",
                (self.owner, digest),
            ).fetchone()
            if existing is not None:
                existing_path = self._path(cast(str, existing["storage_path"]))
                if existing_path is not None and not existing_path.is_file():
                    # The row is authoritative; restore a lost original.
                    atomic_write_private_bytes(existing_path, data)
                return _document(existing), False
            document = Document(
                owner=self.owner,
                kind=DOCUMENT_KIND_UPLOAD,
                title=name,
                media_type=media,
                sha256=digest,
                size_bytes=len(data),
                metadata={"original_name": name},
            )
            stored_name = f"{digest}-{document.document_id}"
            atomic_write_private_bytes(self.root / stored_name, data)
            connection.execute(
                "INSERT INTO documents (document_id, owner, kind, title, "
                "media_type, sha256, size_bytes, source_url, metadata, status, "
                "embedding_status, graph_status, storage_path, created_at, "
                "updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, "
                "%s, %s, %s)",
                (
                    document.document_id,
                    document.owner,
                    document.kind,
                    document.title,
                    document.media_type,
                    document.sha256,
                    document.size_bytes,
                    source_url,
                    json.dumps(document.metadata, ensure_ascii=False),
                    document.status,
                    document.embedding_status,
                    document.graph_status,
                    stored_name,
                    document.created_at,
                    document.updated_at,
                ),
            )
            return document, True

    def get(self, document_id: str) -> Document | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE owner = %s AND document_id = %s",
                (self.owner, document_id),
            ).fetchone()
        return None if row is None else _document(row)

    def list_documents(
        self, *, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0
    ) -> tuple[Document, ...]:
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents WHERE owner = %s "
                "ORDER BY created_at DESC, document_id DESC LIMIT %s OFFSET %s",
                (self.owner, limit, offset),
            ).fetchall()
        return tuple(_document(row) for row in rows)

    def delete(self, document_id: str) -> bool:
        """Delete the row, its chunks/index and the stored original."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT storage_path FROM documents "
                "WHERE owner = %s AND document_id = %s FOR UPDATE",
                (self.owner, document_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM documents WHERE owner = %s AND document_id = %s",
                (self.owner, document_id),
            )
            connection.execute(
                "DELETE FROM entities AS entity WHERE entity.owner = %s "
                "AND NOT EXISTS (SELECT 1 FROM chunk_entities AS mention "
                "WHERE mention.entity_id = entity.entity_id) "
                "AND NOT EXISTS (SELECT 1 FROM relations AS relation "
                "WHERE relation.source_entity_id = entity.entity_id "
                "OR relation.target_entity_id = entity.entity_id)",
                (self.owner,),
            )
            stored_name = cast(str, row["storage_path"])
            # Older rows may share their hash-only original with another owner.
            still_referenced = connection.execute(
                "SELECT 1 FROM documents WHERE storage_path = %s LIMIT 1",
                (stored_name,),
            ).fetchone()
        path = self._path(stored_name)
        if path is not None and still_referenced is None:
            path.unlink(missing_ok=True)
        return True

    def retry(self, document_id: str) -> Document | None:
        """Return one failed or finished document to the queue."""
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE documents SET status = %s, error = NULL, "
                "embedding_status = %s, graph_status = %s, attempts = 0, "
                "claimed_at = NULL, processed_at = NULL, updated_at = now() "
                "WHERE owner = %s AND document_id = %s AND status <> %s "
                "RETURNING *",
                (
                    DOCUMENT_PENDING,
                    STEP_PENDING,
                    STEP_PENDING,
                    self.owner,
                    document_id,
                    DOCUMENT_PROCESSING,
                ),
            ).fetchone()
        return None if row is None else _document(row)

    def claim_next(self) -> Document | None:
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE documents SET status = %s, attempts = attempts + 1, "
                "claimed_at = now(), updated_at = now() "
                "WHERE document_id = ("
                "SELECT document_id FROM documents WHERE status = %s AND owner = %s "
                "ORDER BY created_at, document_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING *",
                (DOCUMENT_PROCESSING, DOCUMENT_PENDING, self.owner),
            ).fetchone()
        return None if row is None else _document(row)

    def requeue_stale(self) -> int:
        """Reset rows where a previous Server process died mid-ingestion."""
        with self._connect() as connection:
            rows = connection.execute(
                "UPDATE documents SET status = %s, claimed_at = NULL, "
                "updated_at = now() WHERE status = %s AND owner = %s "
                "RETURNING document_id",
                (DOCUMENT_PENDING, DOCUMENT_PROCESSING, self.owner),
            ).fetchall()
        return len(rows)

    def recover(self) -> int:
        """Requeue interrupted work and remove files no row references."""
        requeued = self.requeue_stale()
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"document-originals:{self.root}",),
            )
            rows = connection.execute("SELECT storage_path FROM documents").fetchall()
            referenced = {row["storage_path"] for row in rows}
            for path in self.root.iterdir():
                if (
                    _ORIGINAL_NAME.fullmatch(path.name)
                    and path.name not in referenced
                    and path.is_file()
                ):
                    path.unlink(missing_ok=True)
        return requeued

    def original_path(self, document_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT storage_path FROM documents "
                "WHERE owner = %s AND document_id = %s",
                (self.owner, document_id),
            ).fetchone()
        return None if row is None else self._path(cast(str, row["storage_path"]))

    def document_text(
        self,
        document_id: str,
        *,
        offset: int = 0,
        max_chars: int = MAX_DOCUMENT_TEXT_CHARS,
    ) -> DocumentTextPage | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT d.*, substring(t.text FROM %s FOR %s) AS content, "
                "char_length(t.text) AS total_chars, t.parser, t.parser_version "
                "FROM documents d JOIN document_texts t USING (document_id) "
                "WHERE d.owner = %s AND d.document_id = %s",
                (offset + 1, max_chars, self.owner, document_id),
            ).fetchone()
        if row is None:
            return None
        return DocumentTextPage(
            document=_document(row),
            content=cast(str, row["content"]),
            parser=cast(str, row["parser"]),
            parser_version=cast(str, row["parser_version"]),
            offset=offset,
            total_chars=int(row["total_chars"]),
        )

    def save_extraction(
        self,
        document_id: str,
        text: str,
        parser: str,
        parser_version: str,
        chunks: Sequence[TextChunk],
    ) -> tuple[int, ...]:
        """Keep the chunk IDs and embeddings when extraction is unchanged."""
        with self._connect() as connection:
            unchanged = connection.execute(
                "SELECT 1 FROM document_texts WHERE document_id = %s AND text = %s "
                "AND parser = %s AND parser_version = %s",
                (document_id, text, parser, parser_version),
            ).fetchone()
            if unchanged is not None:
                existing = connection.execute(
                    "SELECT chunk_id, text, token_count, heading_path, locator "
                    "FROM chunks WHERE document_id = %s ORDER BY ordinal",
                    (document_id,),
                ).fetchall()
                if len(existing) == len(chunks) and all(
                    row["text"] == chunk.text
                    and row["token_count"] == chunk.token_count
                    and tuple(row["heading_path"] or ()) == (chunk.heading_path or ())
                    and row["locator"]
                    == {
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                    }
                    for row, chunk in zip(existing, chunks, strict=True)
                ):
                    return tuple(int(row["chunk_id"]) for row in existing)
            connection.execute(
                "DELETE FROM relations WHERE document_id = %s", (document_id,)
            )
            connection.execute(
                "DELETE FROM chunks WHERE document_id = %s", (document_id,)
            )
            connection.execute(
                "INSERT INTO document_texts "
                "(document_id, text, parser, parser_version, extracted_at) "
                "VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (document_id) DO UPDATE SET text = EXCLUDED.text, "
                "parser = EXCLUDED.parser, parser_version = EXCLUDED.parser_version, "
                "extracted_at = now()",
                (document_id, text, parser, parser_version),
            )
            chunk_ids: list[int] = []
            for ordinal, chunk in enumerate(chunks):
                row = connection.execute(
                    "INSERT INTO chunks (document_id, ordinal, text, token_count, "
                    "heading_path, locator) VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                    "RETURNING chunk_id",
                    (
                        document_id,
                        ordinal,
                        chunk.text,
                        chunk.token_count,
                        list(chunk.heading_path) if chunk.heading_path else None,
                        json.dumps(
                            {
                                "char_start": chunk.char_start,
                                "char_end": chunk.char_end,
                            }
                        ),
                    ),
                ).fetchone()
                assert row is not None
                chunk_ids.append(int(row["chunk_id"]))
        return tuple(chunk_ids)

    def missing_embeddings(
        self, document_id: str, model: str, *, limit: int
    ) -> tuple[MissingChunk, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chunk_id, text FROM chunks WHERE document_id = %s "
                "AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %s) "
                "ORDER BY ordinal LIMIT %s",
                (document_id, model, limit),
            ).fetchall()
        return tuple(
            MissingChunk(int(row["chunk_id"]), cast(str, row["text"])) for row in rows
        )

    def save_embeddings(self, model: str, items: Sequence[EmbeddedChunk]) -> None:
        with self._connect() as connection:
            for item in items:
                connection.execute(
                    "UPDATE chunks SET embedding = %s::vector, embedding_model = %s, "
                    "embedding_sha = %s, embedded_at = now() WHERE chunk_id = %s",
                    (
                        vector_literal(item.vector),
                        model,
                        hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
                        item.chunk_id,
                    ),
                )

    def save_graph(
        self,
        document_id: str,
        entities: Sequence[EntityRecord],
        relations: Sequence[RelationRecord],
    ) -> None:
        """Replace this document's entity mentions and relations."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM relations WHERE document_id = %s", (document_id,)
            )
            connection.execute(
                "DELETE FROM chunk_entities WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE document_id = %s)",
                (document_id,),
            )
            entity_ids: dict[str, int] = {}
            for entity in entities:
                normalized = _normalized(entity.name)
                if not normalized or not entity.chunk_ids:
                    continue
                row = connection.execute(
                    "INSERT INTO entities (owner, name, normalized_name, kind) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (owner, normalized_name, kind) DO UPDATE "
                    "SET name = EXCLUDED.name RETURNING entity_id",
                    (self.owner, entity.name, normalized, entity.kind),
                ).fetchone()
                assert row is not None
                entity_id = int(row["entity_id"])
                entity_ids.setdefault(normalized, entity_id)
                for chunk_id in entity.chunk_ids:
                    connection.execute(
                        "INSERT INTO chunk_entities (chunk_id, entity_id, weight) "
                        "VALUES (%s, %s, 1) ON CONFLICT DO NOTHING",
                        (chunk_id, entity_id),
                    )
            for relation in relations:
                source_id = entity_ids.get(_normalized(relation.source))
                target_id = entity_ids.get(_normalized(relation.target))
                if source_id is None or target_id is None or source_id == target_id:
                    continue
                connection.execute(
                    "INSERT INTO relations (owner, source_entity_id, "
                    "target_entity_id, relation, document_id, chunk_id, weight) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 1) "
                    "ON CONFLICT (document_id, source_entity_id, target_entity_id, "
                    "relation) DO NOTHING",
                    (
                        self.owner,
                        source_id,
                        target_id,
                        relation.relation,
                        document_id,
                        relation.chunk_id,
                    ),
                )

    def register_embedding_space(self, model: str, dims: int) -> str:
        if dims < 1:
            raise ValueError("embedding dimensions must be positive")
        name = f"{model}:{dims}"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT dims FROM embedding_spaces WHERE model = %s", (model,)
            ).fetchall()
            if any(int(row["dims"]) != dims for row in rows):
                raise EmbeddingSpaceMismatch(
                    "embedding dimensions changed for this model; "
                    "re-ingest documents before using it"
                )
            connection.execute(
                "INSERT INTO embedding_spaces (name, model, dims, metric) "
                "VALUES (%s, %s, %s, 'cosine') ON CONFLICT (name) DO NOTHING",
                (name, model, dims),
            )
        return name

    def finish(
        self,
        document_id: str,
        *,
        status: str,
        error: str | None,
        embedding_status: str,
        graph_status: str,
    ) -> None:
        if status not in _TERMINAL:
            raise ValueError("finish requires a terminal document status")
        with self._connect() as connection:
            connection.execute(
                "UPDATE documents SET status = %s, error = %s, embedding_status = %s, "
                "graph_status = %s, claimed_at = NULL, processed_at = now(), "
                "updated_at = now() WHERE owner = %s AND document_id = %s",
                (
                    status,
                    error,
                    embedding_status,
                    graph_status,
                    self.owner,
                    document_id,
                ),
            )

    def _path(self, stored_name: str) -> Path | None:
        if not _ORIGINAL_NAME.fullmatch(stored_name):
            return None
        path = self.root / stored_name
        return None if path.is_symlink() else path


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_OWNER",
    "DocumentStore",
    "EmbeddedChunk",
    "EmbeddingSpaceMismatch",
    "EntityRecord",
    "MissingChunk",
    "RelationRecord",
    "vector_literal",
]
