"""Knowledge clips stay verbatim, bounded and durable across restarts."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cairn.knowledge.models import (
    CLIP_CONTENT_MAX_LENGTH,
    CLIP_REQUEST_ID_MAX_LENGTH,
    CLIP_TAG_MAX_LENGTH,
    CLIP_TAGS_MAX_COUNT,
    content_hash,
)
from cairn.knowledge.store import KnowledgeRequestConflict, KnowledgeStore


def test_create_preserves_original_text_and_derives_hash(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    original = "  第一行\n\n```python\nprint('kept')\n```\n"
    clip, created = store.create(
        original,
        source="web",
        tags=[" alpha ", "alpha", "beta"],
        request_id="create-1",
    )
    assert created is True
    assert clip.content == original
    assert clip.content_hash == hashlib.sha256(original.encode()).hexdigest()
    assert clip.content_hash == content_hash(original)
    assert clip.source == "web"
    assert clip.tags == ("alpha", "beta")
    assert clip.archived_at is None
    fetched = store.get(clip.clip_id)
    assert fetched == clip


def test_create_is_idempotent_per_request_id(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    first, created = store.create("same payload", tags=["one"], request_id="stable")
    replay, replayed = store.create("same payload", tags=["one"], request_id="stable")
    assert created is True
    assert replayed is False
    assert replay.clip_id == first.clip_id
    assert len(store.list_clips()) == 1

    with pytest.raises(KnowledgeRequestConflict):
        store.create("different payload", tags=["one"], request_id="stable")

    other, _ = store.create("same payload", tags=["one"], request_id="stable-2")
    assert other.clip_id != first.clip_id
    assert len(store.list_clips()) == 2


def test_concurrent_create_keeps_one_clip_per_request_id(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.create("parallel payload", request_id="concurrent"),
                range(8),
            )
        )
    assert len({clip.clip_id for clip, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len(store.list_clips()) == 1


def test_update_recomputes_hash_and_archive_round_trips(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    clip, _ = store.create("original", request_id="update-1")
    updated = store.update(
        clip.clip_id,
        content="edited by the owner",
        source="cli",
        tags=[" updated "],
    )
    assert updated.content == "edited by the owner"
    assert updated.source == "cli"
    assert updated.tags == ("updated",)
    assert updated.content_hash == content_hash("edited by the owner")
    assert updated.updated_at >= clip.updated_at
    assert store.get(clip.clip_id) == updated

    archived = store.set_archived(clip.clip_id, archived=True)
    assert archived.archived_at is not None
    assert store.list_clips() == ()
    assert store.list_clips(include_archived=True) == (archived,)
    still_archived = store.set_archived(clip.clip_id, archived=True)
    assert still_archived.archived_at == archived.archived_at
    restored = store.set_archived(clip.clip_id, archived=False)
    assert restored.archived_at is None
    assert store.list_clips() == (restored,)


def test_list_searches_content_and_tags_and_pages(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    first, _ = store.create("A needle in the text", tags=["rough"], request_id="a")
    second, _ = store.create("A different note", tags=["Needle-tag"], request_id="b")
    store.create("unrelated", request_id="c")
    assert [clip.clip_id for clip in store.list_clips(query="needle")] == [
        second.clip_id,
        first.clip_id,
    ]
    assert [clip.clip_id for clip in store.list_clips(query="needle-tag")] == [
        second.clip_id
    ]
    assert len(store.list_clips(limit=2, offset=1)) == 2
    with pytest.raises(ValueError):
        store.list_clips(limit=0)
    with pytest.raises(ValueError):
        store.list_clips(limit=201)
    with pytest.raises(ValueError):
        store.list_clips(query="x" * 501)


def test_clips_survive_a_new_store_instance(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    clip, _ = KnowledgeStore(root).create("durable text", request_id="restart")
    reopened = KnowledgeStore(root)
    assert reopened.get(clip.clip_id) == clip


def test_store_rejects_invalid_input_at_the_boundaries(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    for content in ("", "   ", "\n\t"):
        with pytest.raises(ValueError):
            store.create(content)
    with pytest.raises(ValueError):
        store.create("x" * (CLIP_CONTENT_MAX_LENGTH + 1))
    with pytest.raises(ValueError):
        store.create("text", source=" ")
    with pytest.raises(ValueError):
        store.create("text", source="s" * 41)
    with pytest.raises(ValueError):
        store.create("text", tags=["x" * (CLIP_TAG_MAX_LENGTH + 1)])
    with pytest.raises(ValueError):
        store.create("text", tags=[""] * 1)
    with pytest.raises(ValueError):
        store.create(
            "text", tags=[str(index) for index in range(CLIP_TAGS_MAX_COUNT + 1)]
        )
    with pytest.raises(ValueError):
        store.create("text", request_id="r" * (CLIP_REQUEST_ID_MAX_LENGTH + 1))
    with pytest.raises(ValueError):
        store.create("text", request_id="   ")
    with pytest.raises(KeyError):
        store.update("missing", content="x")
    with pytest.raises(KeyError):
        store.set_archived("missing", archived=True)
    clip, _ = store.create("text", request_id="update-bound")
    with pytest.raises(ValueError):
        store.update(clip.clip_id)
    with pytest.raises(ValueError):
        store.update(clip.clip_id, content=" ")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_store_root_and_database_are_private(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    store = KnowledgeStore(root)
    assert root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
