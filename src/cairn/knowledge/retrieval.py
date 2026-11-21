"""Hybrid retrieval over the owner's ready documents.

Three bounded candidate lists — full-text, trigram (the practical path for
unsegmented Chinese) and vector cosine — are fused with reciprocal rank
fusion. A one-hop graph expansion adds chunks that mention entities related to
the best hits. Any optional path failing degrades to the remaining ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from psycopg import Error as PostgresError

from cairn.knowledge.document_store import DocumentStore, vector_literal
from cairn.knowledge.embeddings import EmbeddingClient
from cairn.shared.db import Row

RRF_K = 60
CANDIDATES = 50
EXCERPT_CHARS = 400
GRAPH_SEED_CHUNKS = 5
GRAPH_LIMIT = 20
SEMANTIC_OK = "ok"
SEMANTIC_DISABLED = "disabled"
SEMANTIC_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DocumentHit:
    document_id: str
    chunk_id: int
    title: str
    ordinal: int
    heading_path: tuple[str, ...] | None
    excerpt: str
    matched_by: tuple[str, ...]
    score: float


@dataclass(frozen=True, slots=True)
class DocumentSearchResult:
    hits: tuple[DocumentHit, ...]
    semantic: str


class DocumentSearcher:
    def __init__(
        self, store: DocumentStore, *, embedder: EmbeddingClient | None = None
    ) -> None:
        self.store = store
        self.embedder = embedder

    async def search(self, query: str, *, limit: int = 8) -> DocumentSearchResult:
        needle = query.strip()
        if not needle or limit < 1:
            return DocumentSearchResult((), SEMANTIC_OK)
        lexical = await asyncio.to_thread(self._lexical, needle)
        trigram = await asyncio.to_thread(self._trigram, needle)
        semantic = SEMANTIC_DISABLED
        vector: tuple[Row, ...] = ()
        if self.embedder is not None:
            try:
                vectors = await self.embedder.embed([needle])
                if not vectors:
                    raise ValueError("embedding provider returned no vector")
                vector = await asyncio.to_thread(self._vector, vectors[0])
                semantic = SEMANTIC_OK
            except Exception:
                # The provider and vector dimension mismatches must not break
                # the lexical answer.
                semantic = SEMANTIC_UNAVAILABLE
        fused = _fuse(
            (("lexical", lexical), ("trigram", trigram), ("semantic", vector))
        )
        hits = [
            _hit(row, needle, score, matched) for row, score, matched in fused[:limit]
        ]
        if hits and limit > 1:
            try:
                graph_rows = await asyncio.to_thread(
                    self._graph,
                    [hit.chunk_id for hit in hits[: min(GRAPH_SEED_CHUNKS, limit - 1)]],
                    [hit.chunk_id for hit in hits],
                )
            except PostgresError:
                graph_rows = ()
            if graph_rows:
                # Keep the highest-ranked direct hits and make room for expansion.
                hits = hits[: limit - 1]
                hits.extend(_hit(row, needle, 0.0, ("graph",)) for row in graph_rows)
        return DocumentSearchResult(tuple(hits[:limit]), semantic)

    def recall(self, query: str, *, limit: int = 8) -> tuple[DocumentHit, ...]:
        """Synchronous local recall for review collection.

        Review generation runs collection in a worker thread and must stay
        deterministic. This uses the same lexical, trigram and graph paths as
        interactive search but deliberately skips the embedding provider.
        """
        needle = query.strip()
        if not needle or limit < 1:
            return ()
        lexical = self._lexical(needle)
        trigram = self._trigram(needle)
        fused = _fuse((("lexical", lexical), ("trigram", trigram)))
        hits = [
            _hit(row, needle, score, matched) for row, score, matched in fused[:limit]
        ]
        if hits and limit > 1:
            try:
                graph_rows = self._graph(
                    [hit.chunk_id for hit in hits[: min(GRAPH_SEED_CHUNKS, limit - 1)]],
                    [hit.chunk_id for hit in hits],
                )
            except PostgresError:
                graph_rows = ()
            if graph_rows:
                hits = hits[: limit - 1]
                hits.extend(_hit(row, needle, 0.0, ("graph",)) for row in graph_rows)
        return tuple(hits[:limit])

    def _lexical(self, query: str) -> tuple[Row, ...]:
        with self.store.db.connect("knowledge") as connection:
            return tuple(
                connection.execute(
                    "SELECT c.chunk_id, c.document_id, c.ordinal, c.text, "
                    "c.heading_path, d.title, "
                    "ts_rank_cd(c.search, websearch_to_tsquery('simple', %s)) "
                    "AS score FROM chunks c "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "WHERE d.owner = %s AND d.status = 'ready' "
                    "AND c.search @@ websearch_to_tsquery('simple', %s) "
                    "ORDER BY score DESC, c.chunk_id LIMIT %s",
                    (query, self.store.owner, query, CANDIDATES),
                ).fetchall()
            )

    def _trigram(self, query: str) -> tuple[Row, ...]:
        with self.store.db.connect("knowledge") as connection:
            connection.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.15")
            return tuple(
                connection.execute(
                    "SELECT c.chunk_id, c.document_id, c.ordinal, c.text, "
                    "c.heading_path, d.title, word_similarity(%s, c.text) AS score "
                    "FROM chunks c "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "WHERE d.owner = %s AND d.status = 'ready' "
                    "AND %s <%% c.text "
                    "ORDER BY score DESC, c.chunk_id LIMIT %s",
                    (query, self.store.owner, query, CANDIDATES),
                ).fetchall()
            )

    def _vector(self, vector: list[float]) -> tuple[Row, ...]:
        with self.store.db.connect("knowledge") as connection:
            return tuple(
                connection.execute(
                    "SELECT c.chunk_id, c.document_id, c.ordinal, c.text, "
                    "c.heading_path, d.title, "
                    "1 - (c.embedding <=> %s::vector) AS score "
                    "FROM chunks c "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "WHERE d.owner = %s AND d.status = 'ready' "
                    "AND c.embedding IS NOT NULL AND c.embedding_model = %s "
                    "ORDER BY c.embedding <=> %s::vector LIMIT %s",
                    (
                        vector_literal(vector),
                        self.store.owner,
                        self.embedder.model if self.embedder is not None else "",
                        vector_literal(vector),
                        CANDIDATES,
                    ),
                ).fetchall()
            )

    def _graph(
        self, seed_chunk_ids: list[int], direct_chunk_ids: list[int]
    ) -> tuple[Row, ...]:
        if not seed_chunk_ids:
            return ()
        with self.store.db.connect("knowledge") as connection:
            return tuple(
                connection.execute(
                    "WITH seeds AS ("
                    "SELECT DISTINCT ce.entity_id FROM chunk_entities ce "
                    "JOIN chunks c USING (chunk_id) "
                    "JOIN documents d USING (document_id) "
                    "WHERE c.chunk_id = ANY(%s) AND d.owner = %s "
                    "AND d.status = 'ready' AND d.graph_status = 'ready'), "
                    "edges AS ("
                    "SELECT r.source_entity_id, r.target_entity_id FROM relations r "
                    "JOIN documents d USING (document_id) "
                    "WHERE r.owner = %s AND d.owner = r.owner AND d.status = 'ready' "
                    "AND d.graph_status = 'ready'), "
                    "related AS ("
                    "SELECT target_entity_id AS entity_id FROM edges "
                    "WHERE source_entity_id IN (SELECT entity_id FROM seeds) "
                    "UNION "
                    "SELECT source_entity_id AS entity_id FROM edges "
                    "WHERE target_entity_id IN (SELECT entity_id FROM seeds)) "
                    "SELECT DISTINCT c.chunk_id, c.document_id, c.ordinal, "
                    "c.text, c.heading_path, d.title "
                    "FROM chunk_entities ce "
                    "JOIN related r ON r.entity_id = ce.entity_id "
                    "JOIN chunks c ON c.chunk_id = ce.chunk_id "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "WHERE d.owner = %s AND d.status = 'ready' "
                    "AND d.graph_status = 'ready' AND NOT (c.chunk_id = ANY(%s)) "
                    "ORDER BY c.chunk_id LIMIT %s",
                    (
                        seed_chunk_ids,
                        self.store.owner,
                        self.store.owner,
                        self.store.owner,
                        direct_chunk_ids,
                        GRAPH_LIMIT,
                    ),
                ).fetchall()
            )


def _fuse(
    lists: tuple[tuple[str, tuple[Row, ...]], ...],
) -> list[tuple[Row, float, tuple[str, ...]]]:
    scores: dict[int, float] = {}
    matched: dict[int, list[str]] = {}
    rows: dict[int, Row] = {}
    for label, entries in lists:
        for rank, row in enumerate(entries, 1):
            chunk_id = int(row["chunk_id"])
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            rows[chunk_id] = row
            labels = matched.setdefault(chunk_id, [])
            if label not in labels:
                labels.append(label)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [
        (rows[chunk_id], scores[chunk_id], tuple(matched[chunk_id]))
        for chunk_id in ordered
    ]


def _hit(row: Row, query: str, score: float, matched: tuple[str, ...]) -> DocumentHit:
    heading = row["heading_path"]
    return DocumentHit(
        document_id=str(row["document_id"]),
        chunk_id=int(row["chunk_id"]),
        title=str(row["title"]),
        ordinal=int(row["ordinal"]),
        heading_path=tuple(str(item) for item in heading) if heading else None,
        excerpt=_excerpt(str(row["text"]), query),
        matched_by=matched,
        score=round(score, 6),
    )


def _excerpt(text: str, query: str, limit: int = EXCERPT_CHARS) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    folded = compact.casefold()
    needle = query.strip()
    index = folded.find(needle.casefold()) if needle else -1
    if index < 0:
        for token in needle.split():
            if len(token) >= 2:
                index = folded.find(token.casefold())
                if index >= 0:
                    break
    if index < 0:
        return compact[:limit]
    start = max(0, index - limit // 3)
    end = min(len(compact), start + limit)
    start = max(0, end - limit)
    return (
        ("…" if start else "")
        + compact[start:end]
        + ("…" if end < len(compact) else "")
    )


__all__ = [
    "SEMANTIC_DISABLED",
    "SEMANTIC_OK",
    "SEMANTIC_UNAVAILABLE",
    "DocumentHit",
    "DocumentSearchResult",
    "DocumentSearcher",
]
