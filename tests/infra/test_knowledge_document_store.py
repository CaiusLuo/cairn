"""Document library storage: dedupe, claims, chunks and lifecycle."""

from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from cairn.knowledge.document_store import (
    DocumentStore,
    EmbeddedChunk,
    EmbeddingSpaceMismatch,
)
from cairn.knowledge.documents import (
    DOCUMENT_FAILED,
    DOCUMENT_PENDING,
    DOCUMENT_PROCESSING,
    DOCUMENT_READY,
    STEP_FAILED,
    STEP_READY,
    STEP_SKIPPED,
    chunk_text,
)
from tests.support.pg import database_for


def _store(tmp_path: Path, *, owner: str = "owner") -> DocumentStore:
    database = database_for(tmp_path)
    return DocumentStore(database, tmp_path / f"library-{owner}", owner=owner)


def test_upload_dedupes_by_content_and_keeps_the_first_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, created = store.create_upload("笔记.md", "text/markdown", b"# title\n")
    again, created_again = store.create_upload(
        "renamed.md", "text/markdown", b"# title\n"
    )

    assert created is True
    assert created_again is False
    assert again.document_id == first.document_id
    assert again.title == "笔记.md"
    assert len(store.list_documents()) == 1
    stored = store.original_path(first.document_id)
    assert stored is not None and stored.is_file()
    assert stored.read_bytes() == b"# title\n"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_missing_original_is_restored_on_the_next_upload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("a.txt", "text/plain", b"same bytes")
    path = store.original_path(document.document_id)
    assert path is not None
    path.unlink()

    again, created = store.create_upload("a.txt", "text/plain", b"same bytes")

    assert created is False
    assert again.document_id == document.document_id
    assert path.is_file()


def test_upload_rejects_empty_unsupported_and_oversized_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        store.create_upload("a.txt", "text/plain", b"")
    with pytest.raises(ValueError, match="Unsupported"):
        store.create_upload("payload.exe", "application/octet-stream", b"MZ")
    small = DocumentStore(
        database_for(tmp_path), tmp_path / "small", max_upload_bytes=4
    )
    with pytest.raises(ValueError, match="upload limit"):
        small.create_upload("a.txt", "text/plain", b"12345")


def test_concurrent_uploads_of_the_same_bytes_create_one_document(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda index: store.create_upload(
                    f"copy-{index}.txt", "text/plain", b"same bytes"
                ),
                range(4),
            )
        )
    assert len({document.document_id for document, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


def test_claim_requeue_and_retry_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("a.txt", "text/plain", b"hello")

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.document_id == document.document_id
    assert claimed.status == DOCUMENT_PROCESSING
    assert claimed.attempts == 1
    assert store.claim_next() is None

    assert store.requeue_stale() == 1
    again = store.claim_next()
    assert again is not None and again.attempts == 2

    store.finish(
        again.document_id,
        status=DOCUMENT_FAILED,
        error="extraction failed",
        embedding_status=STEP_FAILED,
        graph_status=STEP_FAILED,
    )
    failed = store.get(document.document_id)
    assert failed is not None and failed.error == "extraction failed"

    retried = store.retry(document.document_id)
    assert retried is not None
    assert retried.status == DOCUMENT_PENDING
    assert retried.error is None
    assert retried.attempts == 0


def test_extraction_chunks_and_embeddings_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("guide.md", "text/markdown", b"placeholder")
    text = "# 指南\n\n" + "第一段内容。第二段内容。" * 50
    chunks = chunk_text(text, chunk_chars=100, overlap=10, markdown=True)
    chunk_ids = store.save_extraction(document.document_id, text, "text", "1", chunks)

    assert len(chunk_ids) == len(chunks)
    missing = store.missing_embeddings(document.document_id, "fake-model", limit=100)
    assert [item.chunk_id for item in missing] == list(chunk_ids)

    store.save_embeddings(
        "fake-model",
        [
            EmbeddedChunk(item.chunk_id, item.text, [0.25, 0.5, 0.25])
            for item in missing
        ],
    )
    assert store.missing_embeddings(document.document_id, "fake-model", limit=100) == ()
    # A different model needs its own vectors for the same chunks.
    assert store.missing_embeddings(document.document_id, "other-model", limit=100)

    stored = store.document_text(document.document_id)
    assert stored is not None
    assert stored.document.document_id == document.document_id
    assert stored.content == text

    # Re-ingestion replaces chunks rather than duplicating them.
    rewritten = chunk_text("短文本。", chunk_chars=100, overlap=0)
    second_ids = store.save_extraction(
        document.document_id, "短文本。", "text", "1", rewritten
    )
    assert len(second_ids) == 1
    assert set(second_ids).isdisjoint(chunk_ids)


def test_retry_after_failure_records_ready_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("a.txt", "text/plain", b"hello")
    store.claim_next()
    store.finish(
        document.document_id,
        status=DOCUMENT_READY,
        error=None,
        embedding_status=STEP_READY,
        graph_status=STEP_SKIPPED,
    )
    done = store.get(document.document_id)
    assert done is not None
    assert done.status == DOCUMENT_READY
    assert done.processed_at is not None


def test_embedding_space_registration_is_stable_and_checked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.register_embedding_space("qwen-embedding", 8) == "qwen-embedding:8"
    assert store.register_embedding_space("qwen-embedding", 8) == "qwen-embedding:8"
    with pytest.raises(EmbeddingSpaceMismatch):
        store.register_embedding_space("qwen-embedding", 16)


def test_delete_removes_row_chunks_and_original(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("a.txt", "text/plain", b"hello")
    text = "hello world"
    store.save_extraction(
        document.document_id,
        text,
        "text",
        "1",
        chunk_text(text, chunk_chars=64, overlap=0),
    )
    path = store.original_path(document.document_id)
    assert path is not None

    assert store.delete(document.document_id) is True
    assert not path.exists()
    assert store.get(document.document_id) is None
    assert store.document_text(document.document_id) is None
    assert store.delete(document.document_id) is False


def test_owners_are_isolated(tmp_path: Path) -> None:
    database = database_for(tmp_path)
    alice = DocumentStore(database, tmp_path / "alice", owner="alice")
    bob = DocumentStore(database, tmp_path / "bob", owner="bob")
    document, _ = alice.create_upload("a.txt", "text/plain", b"alice only")

    assert alice.get(document.document_id) is not None
    assert bob.get(document.document_id) is None
    assert bob.list_documents() == ()
    assert bob.claim_next() is None
    assert bob.delete(document.document_id) is False


def test_concurrent_claims_are_disjoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    documents = [
        store.create_upload(
            f"note-{index}.txt", "text/plain", f"text {index}".encode()
        )[0]
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: store.claim_next(), range(2)))
    assert {item.document_id for item in claimed if item is not None} == {
        document.document_id for document in documents
    }


def test_recover_requeues_claims_and_removes_orphan_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("a.txt", "text/plain", b"hello")
    store.claim_next()
    orphan = store.root / ("0" * 64)
    orphan.write_bytes(b"left by a crashed upload")

    unrelated = store.root / "keep.txt"
    unrelated.write_text("not managed by the document store")

    assert store.recover() == 1
    assert not orphan.exists()
    assert unrelated.read_text() == "not managed by the document store"
    requeued = store.get(document.document_id)
    assert requeued is not None
    assert requeued.status == DOCUMENT_PENDING


def test_reupload_survives_concurrent_delete_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    document, _ = store.create_upload("note.txt", "text/plain", b"hello")
    original = store.original_path(document.document_id)
    assert original is not None
    unlink = Path.unlink
    deleting = Event()
    release = Event()

    def wait_before_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == original:
            deleting.set()
            assert release.wait(5)
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", wait_before_unlink)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deleted = pool.submit(store.delete, document.document_id)
        try:
            assert deleting.wait(5)
            replacement, created = store.create_upload(
                "note.txt", "text/plain", b"hello"
            )
        finally:
            release.set()
        assert deleted.result(timeout=5)
    assert created and replacement.document_id != document.document_id
    new_path = store.original_path(replacement.document_id)
    assert new_path is not None and new_path.read_bytes() == b"hello"


def test_deleting_one_owners_copy_keeps_the_other_original(tmp_path: Path) -> None:
    alice = _store(tmp_path, owner="alice")
    bob = DocumentStore(alice.db, alice.root, owner="bob")
    first, _ = alice.create_upload("note.txt", "text/plain", b"same bytes")
    second, _ = bob.create_upload("note.txt", "text/plain", b"same bytes")

    assert alice.delete(first.document_id)
    original = bob.original_path(second.document_id)
    assert original is not None and original.read_bytes() == b"same bytes"
