"""Document ingestion: success, degradation, restart and shutdown."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from openpyxl import Workbook
from psycopg import DataError

from cairn.knowledge.document_store import DocumentStore, EmbeddedChunk
from cairn.knowledge.documents import (
    DOCUMENT_FAILED,
    DOCUMENT_READY,
    STEP_FAILED,
    STEP_READY,
    STEP_SKIPPED,
)
from cairn.knowledge.graph import GraphEntity, GraphPayload, GraphRelation
from cairn.knowledge.ingest import IngestionSettings, IngestionWorker
from tests.support.knowledge_fakes import (
    FailingEmbedder,
    FailingGraphExtractor,
    FakeEmbedder,
    FakeGraphExtractor,
)
from tests.support.pg import database_for

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "# 部署\n\nCairn 使用 PostgreSQL 保存知识。备份脚本每天运行。"


def _store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(database_for(tmp_path), tmp_path / "library")


def _graph() -> FakeGraphExtractor:
    return FakeGraphExtractor(
        GraphPayload(
            entities=(
                GraphEntity("Cairn", "project", (0,)),
                GraphEntity("PostgreSQL", "concept", (0,)),
            ),
            relations=(GraphRelation("Cairn", "PostgreSQL", "uses", 0),),
        )
    )


def test_ingestion_embeds_chunks_and_builds_a_graph(tmp_path: Path) -> None:
    database = database_for(tmp_path)
    store = DocumentStore(database, tmp_path / "library")
    embedder = FakeEmbedder(dims=8)
    graph = _graph()
    worker = IngestionWorker(store, embedder=embedder, graph=graph)
    document, _ = store.create_upload("notes.md", "text/markdown", TEXT.encode())

    async def scenario() -> bool:
        assert await worker.process_one() is True
        return await worker.process_one()

    assert asyncio.run(scenario()) is False
    stored = store.get(document.document_id)
    assert stored is not None
    assert stored.status == DOCUMENT_READY
    assert stored.embedding_status == STEP_READY
    assert stored.graph_status == STEP_READY
    assert embedder.calls and graph.calls

    with database.connect("knowledge") as connection:
        chunks = connection.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s",
            (document.document_id,),
        ).fetchone()
        assert chunks is not None and chunks["n"] > 0
        embedded = connection.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s "
            "AND embedding IS NOT NULL AND embedding_model = %s",
            (document.document_id, "fake-embedding"),
        ).fetchone()
        assert embedded is not None and embedded["n"] == chunks["n"]
        entities = connection.execute(
            "SELECT count(*) AS n FROM entities WHERE owner = %s", ("owner",)
        ).fetchone()
        assert entities is not None and entities["n"] == 2
        mentions = connection.execute(
            "SELECT count(*) AS n FROM chunk_entities"
        ).fetchone()
        assert mentions is not None and mentions["n"] >= 2
        relations = connection.execute(
            "SELECT count(*) AS n FROM relations WHERE document_id = %s",
            (document.document_id,),
        ).fetchone()
        assert relations is not None and relations["n"] == 1
        space = connection.execute(
            "SELECT dims FROM embedding_spaces WHERE model = %s",
            ("fake-embedding",),
        ).fetchone()
        assert space is not None and space["dims"] == 8


def test_embedding_failure_keeps_the_document_searchable(tmp_path: Path) -> None:
    database = database_for(tmp_path)
    store = DocumentStore(database, tmp_path / "library")
    worker = IngestionWorker(store, embedder=FailingEmbedder())
    document, _ = store.create_upload("notes.md", "text/markdown", TEXT.encode())

    asyncio.run(worker.process_one())

    stored = store.get(document.document_id)
    assert stored is not None
    assert stored.status == DOCUMENT_READY
    assert stored.embedding_status == STEP_FAILED
    assert stored.graph_status == STEP_SKIPPED
    with database.connect("knowledge") as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s",
            (document.document_id,),
        ).fetchone()
        assert row is not None and row["n"] > 0


def test_graph_failure_does_not_block_the_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    worker = IngestionWorker(
        store, embedder=FakeEmbedder(), graph=FailingGraphExtractor()
    )
    document, _ = store.create_upload("notes.md", "text/markdown", TEXT.encode())

    asyncio.run(worker.process_one())

    stored = store.get(document.document_id)
    assert stored is not None
    assert stored.status == DOCUMENT_READY
    assert stored.embedding_status == STEP_READY
    assert stored.graph_status == STEP_FAILED


def test_offline_mode_still_builds_searchable_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    worker = IngestionWorker(store)
    document, _ = store.create_upload("notes.md", "text/markdown", TEXT.encode())

    asyncio.run(worker.process_one())

    stored = store.get(document.document_id)
    assert stored is not None
    assert stored.status == DOCUMENT_READY
    assert stored.embedding_status == STEP_SKIPPED
    assert stored.graph_status == STEP_SKIPPED
    assert store.document_text(document.document_id) is not None


def test_unparseable_upload_fails_with_a_safe_message(tmp_path: Path) -> None:
    store = _store(tmp_path)
    worker = IngestionWorker(store)
    document, _ = store.create_upload("broken.docx", DOCX_MEDIA, b"not a zip")

    asyncio.run(worker.process_one())

    stored = store.get(document.document_id)
    assert stored is not None
    assert stored.status == DOCUMENT_FAILED
    assert stored.error is not None
    assert "could not be parsed" in stored.error
    assert stored.attempts == 1
    assert store.document_text(document.document_id) is None


def test_start_recovers_interrupted_claims_and_ingests_new_uploads(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    embedder = FakeEmbedder()
    interrupted, _ = store.create_upload("first.md", "text/markdown", b"first document")
    store.claim_next()  # a previous process died mid-ingestion

    async def wait_ready(document_id: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stored = store.get(document_id)
            if stored is not None and stored.status == DOCUMENT_READY:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"document {document_id} was not ingested")

    async def scenario() -> None:
        worker = IngestionWorker(store, embedder=embedder, poll_interval=0.05)
        worker.start()
        try:
            await wait_ready(interrupted.document_id)
            fresh, _ = store.create_upload(
                "second.md", "text/markdown", b"second document"
            )
            worker.notify()
            await wait_ready(fresh.document_id)
        finally:
            await worker.close()

    asyncio.run(scenario())
    first = store.get(interrupted.document_id)
    assert first is not None
    assert first.attempts == 2


def test_restart_keeps_a_committed_embedding_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    text = "\n\n".join(f"Paragraph {i:02d}: " + "more words " * 5 for i in range(12))
    document, _ = store.create_upload("notes.txt", "text/plain", text.encode())
    settings = IngestionSettings(
        chunk_chars=64, chunk_overlap=0, embedding_batch_size=2
    )

    class InterruptedEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if self.calls:
                raise asyncio.CancelledError
            return await super().embed(texts)

    before = InterruptedEmbedder()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            IngestionWorker(store, embedder=before, settings=settings).process_one()
        )
    missing = store.missing_embeddings(document.document_id, before.model, limit=100)
    assert missing and len(before.calls[0]) == 2

    assert store.recover() == 1
    after = FakeEmbedder()
    asyncio.run(IngestionWorker(store, embedder=after, settings=settings).process_one())

    assert [text for batch in after.calls for text in batch] == [
        item.text for item in missing
    ]
    assert store.missing_embeddings(document.document_id, after.model, limit=100) == ()
    stored = store.get(document.document_id)
    assert stored is not None and stored.status == DOCUMENT_READY


def test_vector_storage_error_leaves_lexical_document_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)

    def reject_vectors(model: str, items: Sequence[EmbeddedChunk]) -> None:
        raise DataError("unusable vector")

    monkeypatch.setattr(store, "save_embeddings", reject_vectors)
    document, _ = store.create_upload("note.txt", "text/plain", b"some content")
    asyncio.run(IngestionWorker(store, embedder=FakeEmbedder()).process_one())

    stored = store.get(document.document_id)
    assert stored is not None and stored.status == DOCUMENT_READY
    assert stored.embedding_status == STEP_FAILED


def test_spreadsheet_uses_the_library_extraction_budget(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for _ in range(50):
        sheet.append(["x" * 1000])
    sheet.append(["spreadsheet last row"])
    path = tmp_path / "data.xlsx"
    workbook.save(path)
    workbook.close()
    store = _store(tmp_path)
    document, _ = store.create_upload("data.xlsx", "", path.read_bytes())

    asyncio.run(IngestionWorker(store).process_one())

    page = store.document_text(document.document_id, offset=49_999, max_chars=2000)
    assert page is not None and "spreadsheet last row" in page.content
