"""Background ingestion of uploaded documents into searchable chunks.

One Server process owns the queue. A crash may happen between steps, so the
store records each document's state: interrupted claims are requeued on start,
and already-embedded chunks are never recomputed. Embedding and graph failures
degrade to lexical search instead of failing the document.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from psycopg import Error as PostgresError

from cairn.knowledge.document_store import (
    DocumentStore,
    EmbeddedChunk,
    EmbeddingSpaceMismatch,
    EntityRecord,
    RelationRecord,
)
from cairn.knowledge.documents import (
    DOCUMENT_FAILED,
    DOCUMENT_READY,
    STEP_FAILED,
    STEP_READY,
    STEP_SKIPPED,
    Document,
    TextChunk,
    chunk_text,
)
from cairn.knowledge.embeddings import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingError,
)
from cairn.knowledge.graph import GraphExtractor, GraphPayload
from cairn.shared.text_extract import ExtractionUnavailable, extract_bytes

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 2.0
MAX_ERROR_CHARS = 300
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    chunk_chars: int = 1200
    chunk_overlap: int = 150
    max_chunks: int = 400
    extraction_max_chars: int = 2_000_000
    max_pdf_pages: int = 500
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    graph_max_chunks: int = 12


class IngestionWorker:
    def __init__(
        self,
        store: DocumentStore,
        *,
        embedder: EmbeddingClient | None = None,
        graph: GraphExtractor | None = None,
        settings: IngestionSettings | None = None,
        poll_interval: float = POLL_SECONDS,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.graph = graph
        self.settings = settings or IngestionSettings()
        self.poll_interval = max(0.05, poll_interval)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self.store.recover()
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self.run(), name="knowledge-ingest")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def notify(self) -> None:
        """Wake the loop after a new upload; safe to call from the event loop."""
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("knowledge ingestion tick failed")
                processed = False
            if not processed:
                await self._wait()

    async def process_one(self) -> bool:
        document = await asyncio.to_thread(self.store.claim_next)
        if document is None:
            return False
        await self._process(document)
        return True

    async def _wait(self) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)

    async def _process(self, document: Document) -> None:
        try:
            await self._ingest(document)
        except asyncio.CancelledError:
            # The row stays 'processing' and is requeued on the next start.
            raise
        except Exception as error:
            LOGGER.warning(
                "knowledge ingestion failed for %s: %s",
                document.document_id,
                type(error).__name__,
            )
            await self._finish_failed(
                document.document_id, f"ingestion failed: {type(error).__name__}"
            )

    async def _ingest(self, document: Document) -> None:
        path = await asyncio.to_thread(self.store.original_path, document.document_id)
        if path is None:
            await self._finish_failed(
                document.document_id, "stored original is missing"
            )
            return
        try:
            data = await asyncio.to_thread(path.read_bytes)
            extracted = await asyncio.to_thread(
                extract_bytes,
                document.title,
                document.media_type,
                document.size_bytes,
                data,
                max_chars=self.settings.extraction_max_chars,
                max_pdf_pages=self.settings.max_pdf_pages,
            )
        except ExtractionUnavailable as error:
            await self._finish_failed(document.document_id, str(error))
            return
        except OSError:
            await self._finish_failed(
                document.document_id, "stored original could not be read"
            )
            return

        text = extracted.text.replace("\x00", "�")  # PostgreSQL text cannot store NUL.
        if len(text) > self.settings.extraction_max_chars:
            text = text[: self.settings.extraction_max_chars]
        chunks = chunk_text(
            text,
            chunk_chars=self.settings.chunk_chars,
            overlap=self.settings.chunk_overlap,
            max_chunks=self.settings.max_chunks,
            markdown=Path(document.title).suffix.lower() in _MARKDOWN_SUFFIXES,
        )
        if not chunks:
            await self._finish_failed(
                document.document_id, "document has no extractable text"
            )
            return
        chunk_ids = await asyncio.to_thread(
            self.store.save_extraction,
            document.document_id,
            text,
            extracted.parser,
            extracted.parser_version,
            chunks,
        )

        embedding_status = STEP_SKIPPED
        if self.embedder is not None:
            try:
                await self._embed(document.document_id)
                embedding_status = STEP_READY
            except (
                EmbeddingError,
                EmbeddingSpaceMismatch,
                PostgresError,
                ValueError,
            ) as error:
                embedding_status = STEP_FAILED
                LOGGER.warning(
                    "knowledge embedding failed for %s: %s",
                    document.document_id,
                    type(error).__name__,
                )

        graph_status = STEP_SKIPPED
        if self.graph is not None:
            try:
                await self._extract_graph(document.document_id, chunks, chunk_ids)
                graph_status = STEP_READY
            except asyncio.CancelledError:
                raise
            except Exception as error:
                graph_status = STEP_FAILED
                LOGGER.warning(
                    "knowledge graph extraction failed for %s: %s",
                    document.document_id,
                    type(error).__name__,
                )

        await asyncio.to_thread(
            self.store.finish,
            document.document_id,
            status=DOCUMENT_READY,
            error=None,
            embedding_status=embedding_status,
            graph_status=graph_status,
        )

    async def _embed(self, document_id: str) -> None:
        embedder = self.embedder
        assert embedder is not None
        while True:
            missing = await asyncio.to_thread(
                self.store.missing_embeddings,
                document_id,
                embedder.model,
                limit=self.settings.embedding_batch_size,
            )
            if not missing:
                return
            vectors = await embedder.embed([item.text for item in missing])
            if len(vectors) != len(missing):
                raise EmbeddingError("embedding response did not match the request")
            await asyncio.to_thread(
                self.store.register_embedding_space, embedder.model, len(vectors[0])
            )
            await asyncio.to_thread(
                self.store.save_embeddings,
                embedder.model,
                [
                    EmbeddedChunk(item.chunk_id, item.text, vector)
                    for item, vector in zip(missing, vectors, strict=True)
                ],
            )

    async def _extract_graph(
        self,
        document_id: str,
        chunks: Sequence[TextChunk],
        chunk_ids: tuple[int, ...],
    ) -> None:
        extractor = self.graph
        assert extractor is not None
        inputs = [
            (index, chunk.text)
            for index, chunk in enumerate(chunks[: self.settings.graph_max_chunks])
        ]
        payload = await extractor.extract(inputs)
        entities, relations = _records(payload, chunk_ids)
        await asyncio.to_thread(self.store.save_graph, document_id, entities, relations)

    async def _finish_failed(self, document_id: str, error: str) -> None:
        await asyncio.to_thread(
            self.store.finish,
            document_id,
            status=DOCUMENT_FAILED,
            error=error[:MAX_ERROR_CHARS],
            embedding_status=STEP_FAILED,
            graph_status=STEP_FAILED,
        )


def _records(
    payload: GraphPayload, chunk_ids: tuple[int, ...]
) -> tuple[tuple[EntityRecord, ...], tuple[RelationRecord, ...]]:
    ordinals = dict(enumerate(chunk_ids))
    entities: list[EntityRecord] = []
    for entity in payload.entities:
        mapped = tuple(
            ordinals[ordinal] for ordinal in entity.chunks if ordinal in ordinals
        )
        if mapped:
            entities.append(EntityRecord(entity.name, entity.kind, mapped))
    relations = [
        RelationRecord(
            relation.source,
            relation.target,
            relation.relation,
            ordinals.get(relation.chunk) if relation.chunk is not None else None,
        )
        for relation in payload.relations
    ]
    return tuple(entities), tuple(relations)


__all__ = ["IngestionSettings", "IngestionWorker"]
