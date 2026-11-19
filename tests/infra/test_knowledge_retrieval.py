"""Hybrid retrieval: lexical paths, vectors, fusion, graph and ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path

from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.embeddings import EmbeddingClient
from cairn.knowledge.graph import (
    GraphEntity,
    GraphExtractor,
    GraphPayload,
    GraphRelation,
)
from cairn.knowledge.ingest import IngestionWorker
from cairn.knowledge.retrieval import (
    SEMANTIC_DISABLED,
    SEMANTIC_OK,
    SEMANTIC_UNAVAILABLE,
    DocumentSearcher,
)
from tests.support.knowledge_fakes import (
    FailingEmbedder,
    FakeEmbedder,
    FakeGraphExtractor,
)
from tests.support.pg import database_for


def _store(tmp_path: Path, *, owner: str = "owner") -> DocumentStore:
    database = database_for(tmp_path)
    return DocumentStore(database, tmp_path / f"library-{owner}", owner=owner)


def _ingest(
    store: DocumentStore,
    text: str,
    *,
    title: str = "notes.md",
    embedder: EmbeddingClient | None = None,
    graph: GraphExtractor | None = None,
) -> str:
    document, _ = store.create_upload(title, "text/markdown", text.encode())
    worker = IngestionWorker(store, embedder=embedder, graph=graph)
    asyncio.run(worker.process_one())
    assert store.get(document.document_id).status == "ready"  # type: ignore[union-attr]
    return document.document_id


def test_chinese_trigram_search_finds_a_document_without_vectors(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_id = _ingest(store, "生产服务器每天凌晨执行数据库备份。")
    searcher = DocumentSearcher(store)

    result = asyncio.run(searcher.search("数据库备份"))

    assert result.semantic == SEMANTIC_DISABLED
    assert result.hits
    assert result.hits[0].document_id == document_id
    assert "trigram" in result.hits[0].matched_by
    assert "数据库备份" in result.hits[0].excerpt


def test_keyword_and_vector_paths_agree_and_are_fused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document_id = _ingest(
        store, "Cairn 使用 PostgreSQL 保存知识。", embedder=FakeEmbedder()
    )
    searcher = DocumentSearcher(store, embedder=FakeEmbedder())

    result = asyncio.run(searcher.search("Cairn PostgreSQL"))

    assert result.semantic == SEMANTIC_OK
    hit = next(hit for hit in result.hits if hit.document_id == document_id)
    assert "lexical" in hit.matched_by
    assert "semantic" in hit.matched_by


def test_embedding_failure_degrades_to_lexical_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _ingest(store, "服务器部署脚本写在仓库里。")
    searcher = DocumentSearcher(store, embedder=FailingEmbedder())

    result = asyncio.run(searcher.search("部署脚本"))

    assert result.semantic == SEMANTIC_UNAVAILABLE
    assert result.hits


def test_graph_expansion_adds_related_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    graph = FakeGraphExtractor(
        GraphPayload(
            entities=(
                GraphEntity("Cairn", "project", (0,)),
                GraphEntity("PostgreSQL", "concept", (0,)),
            ),
            relations=(GraphRelation("Cairn", "PostgreSQL", "uses", 0),),
        )
    )
    target = _ingest(
        store,
        "Cairn 使用 PostgreSQL 保存知识。",
        title="cairn.md",
        graph=graph,
    )
    related = _ingest(
        store,
        "PostgreSQL 每天凌晨执行备份。",
        title="backup.md",
        graph=graph,
    )
    searcher = DocumentSearcher(store)

    result = asyncio.run(searcher.search("Cairn"))

    assert any(hit.document_id == target for hit in result.hits)
    expanded = [hit for hit in result.hits if hit.document_id == related]
    assert expanded
    assert expanded[0].matched_by == ("graph",)


def test_deleted_and_failed_documents_are_not_searchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document_id = _ingest(store, "临时文档提到 独角兽 这个词。")
    store.delete(document_id)

    broken, _ = store.create_upload(
        "broken.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"not a zip",
    )
    asyncio.run(IngestionWorker(store).process_one())

    result = asyncio.run(DocumentSearcher(store).search("独角兽"))

    assert result.hits == ()
    assert store.get(broken.document_id).status == "failed"  # type: ignore[union-attr]


def test_search_is_owner_scoped(tmp_path: Path) -> None:
    database = database_for(tmp_path)
    alice = DocumentStore(database, tmp_path / "alice", owner="alice")
    bob = DocumentStore(database, tmp_path / "bob", owner="bob")
    _ingest(alice, "Alice 的服务器密码提示写在文档里。")

    alice_hits = asyncio.run(DocumentSearcher(alice).search("服务器密码提示"))
    bob_hits = asyncio.run(DocumentSearcher(bob).search("服务器密码提示"))

    assert alice_hits.hits
    assert bob_hits.hits == ()
