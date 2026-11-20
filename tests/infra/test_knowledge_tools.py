"""Owner-only read tools must read real persisted clips, never runtime files."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg import Error as PostgresError

from cairn.agent.tools.base import InvalidArguments, Tool
from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.ingest import IngestionWorker
from cairn.knowledge.retrieval import DocumentSearcher
from cairn.knowledge.store import KnowledgeStore
from cairn.knowledge.tools import (
    DocumentReadTool,
    KnowledgeGetTool,
    KnowledgeListTool,
    KnowledgeSearchTool,
    personal_knowledge_tools,
)
from tests.support.knowledge_fakes import FakeEmbedder
from tests.support.pg import database_for


def result(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    outcome = asyncio.run(tool.execute(args))
    assert outcome.exit_code == 0, outcome.stderr
    return cast(dict[str, Any], json.loads(outcome.stdout))


def test_list_search_get_pagination_and_archived_filter(tmp_path: Path) -> None:
    store = KnowledgeStore(database_for(tmp_path))
    first, _ = store.create(
        "Cairn saved knowledge: blue-shape " + "x" * 9000,
        tags=["cairn"],
    )
    archived, _ = store.create("obsolete blue-shape", tags=["cairn"])
    store.set_archived(archived.clip_id, archived=True)
    store.create("newest note", tags=["journal"])

    listed = result(KnowledgeListTool(store), {"limit": 1})
    assert listed["has_more"] is True
    assert len(listed["clips"]) == 1
    assert "clip_id" in listed["clips"][0]
    next_page = result(KnowledgeListTool(store), {"limit": 1, "offset": 1})
    assert len(next_page["clips"]) == 1

    matches = result(KnowledgeSearchTool(store), {"query": "blue-shape"})
    assert len(matches["clips"]) == 1
    assert matches["clips"][0]["clip_id"] == first.clip_id
    assert matches["clips"][0]["tags"] == ["cairn"]
    assert len(matches["clips"][0]["preview"]) <= 240
    assert matches["untrusted_reference"] is True
    assert listed["untrusted_reference"] is True
    assert result(KnowledgeSearchTool(store), {"query": "absent"})["status"] == "empty"

    detail = result(KnowledgeGetTool(store), {"clip_id": first.clip_id})
    assert detail["clip"]["clip_id"] == first.clip_id
    assert len(detail["content"]) == 4000
    assert detail["total_chars"] == len(first.content)
    assert detail["next_offset"] == 4000
    rest = result(
        KnowledgeGetTool(store),
        {"clip_id": first.clip_id, "offset": detail["next_offset"], "max_chars": 1000},
    )
    assert len(rest["content"]) == 1000
    assert detail["untrusted_reference"] is True
    past_end = result(
        KnowledgeGetTool(store),
        {"clip_id": first.clip_id, "offset": len(first.content) + 10},
    )
    assert past_end["status"] == "ok"
    assert past_end["content"] == ""
    assert past_end["total_chars"] == len(first.content)
    assert past_end["next_offset"] is None

    assert (
        result(KnowledgeGetTool(store), {"clip_id": archived.clip_id})["status"]
        == "not_found"
    )
    assert (
        result(KnowledgeGetTool(store), {"clip_id": "clip-missing"})["status"]
        == "not_found"
    )

    reopened = KnowledgeStore(database_for(tmp_path))
    assert (
        result(KnowledgeGetTool(reopened), {"clip_id": first.clip_id})["status"] == "ok"
    )


def test_chinese_keyword_search_returns_citeable_clip(tmp_path: Path) -> None:
    store = KnowledgeStore(database_for(tmp_path))
    clip, _ = store.create("项目代号: 蓝石。下周与供应商确认交付时间。")
    outcome = result(KnowledgeSearchTool(store), {"query": "蓝石"})
    assert outcome["status"] == "ok"
    assert [item["clip_id"] for item in outcome["clips"]] == [clip.clip_id]
    assert "蓝石" in outcome["clips"][0]["preview"]


def test_tools_validate_model_arguments(tmp_path: Path) -> None:
    store = KnowledgeStore(database_for(tmp_path))
    for tool, args in (
        (KnowledgeListTool(store), {"limit": True}),
        (KnowledgeListTool(store), {"limit": 0}),
        (KnowledgeListTool(store), {"limit": 21}),
        (KnowledgeListTool(store), {"offset": -1}),
        (KnowledgeListTool(store), {"offset": 50_000_001}),
        (KnowledgeListTool(store), {"include_archived": True}),
        (KnowledgeListTool(store), {"path": "/private/state/domain.sqlite3"}),
        (KnowledgeSearchTool(store), {"query": " "}),
        (KnowledgeSearchTool(store), {"query": "x" * 501}),
        (KnowledgeSearchTool(store), {"query": "x", "limit": 0}),
        (KnowledgeSearchTool(store), {"query": "x", "limit": 21}),
        (KnowledgeSearchTool(store), {"query": "x", "offset": -1}),
        (KnowledgeSearchTool(store), {"query": "x", "token": "secret"}),
        (KnowledgeGetTool(store), {"clip_id": ""}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "max_chars": 0}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "max_chars": 5000}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "offset": -1}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "offset": 100001}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "path": "/etc/passwd"}),
    ):
        with pytest.raises(InvalidArguments):
            tool.validate(args)


def _document_library(tmp_path: Path) -> tuple[DocumentStore, str]:
    store = DocumentStore(database_for(tmp_path), tmp_path / "documents")
    document, _ = store.create_upload(
        "runbook.md",
        "text/markdown",
        "# 部署\n\n生产服务器先停 cairn.service, 再执行数据库备份。".encode(),
    )
    asyncio.run(IngestionWorker(store, embedder=FakeEmbedder()).process_one())
    return store, document.document_id


def test_search_includes_document_chunks_and_read_paginates(tmp_path: Path) -> None:
    documents, document_id = _document_library(tmp_path)
    knowledge = KnowledgeStore(database_for(tmp_path))
    tool = KnowledgeSearchTool(knowledge, DocumentSearcher(documents))

    found = result(tool, {"query": "数据库备份"})

    assert found["status"] == "ok"
    assert found["clips"] == []
    assert found["documents"]
    assert found["documents"][0]["document_id"] == document_id
    assert found["documents"][0]["chunk_id"]
    assert found["documents"][0]["title"] == "runbook.md"
    assert "数据库备份" in found["documents"][0]["excerpt"]
    assert found["semantic"] == "disabled"
    assert found["untrusted_reference"] is True

    reader = DocumentReadTool(documents)
    detail = result(reader, {"document_id": document_id, "max_chars": 10})
    assert detail["status"] == "ok"
    assert detail["document"]["title"] == "runbook.md"
    assert detail["document"]["parser"] == "text"
    assert len(detail["content"]) == 10
    assert detail["next_offset"] == 10
    rest = result(reader, {"document_id": document_id, "offset": 10})
    assert rest["offset"] == 10
    assert rest["next_offset"] is None
    assert result(reader, {"document_id": "doc-missing"})["status"] == "not_found"
    assert detail["untrusted_reference"] is True


def test_document_tools_are_only_registered_with_a_library(tmp_path: Path) -> None:
    store = KnowledgeStore(database_for(tmp_path))
    documents, _ = _document_library(tmp_path)

    without = {tool.name for tool in personal_knowledge_tools(store)}
    with_library = {tool.name for tool in personal_knowledge_tools(store, documents)}

    assert "document_read" not in without
    assert with_library - without == {"document_read"}
    assert with_library >= {"knowledge_list", "knowledge_search", "knowledge_get"}


def test_document_tool_arguments_are_validated(tmp_path: Path) -> None:
    reader = DocumentReadTool(DocumentStore(database_for(tmp_path), tmp_path / "d"))
    for args in (
        {"document_id": ""},
        {"document_id": "doc-1", "offset": -1},
        {"document_id": "doc-1", "offset": 50_000_001},
        {"document_id": "doc-1", "max_chars": 0},
        {"document_id": "doc-1", "max_chars": 5000},
        {"document_id": "doc-1", "path": "/etc/passwd"},
    ):
        with pytest.raises(InvalidArguments):
            reader.validate(args)


def test_unavailable_document_store_is_distinct_from_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents, document_id = _document_library(tmp_path)
    searcher = DocumentSearcher(documents)
    knowledge = KnowledgeStore(database_for(tmp_path))

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise PostgresError("secret path /private/documents")

    monkeypatch.setattr(searcher, "search", unavailable)
    outcome = asyncio.run(
        KnowledgeSearchTool(knowledge, searcher).execute({"query": "备份"})
    )
    assert outcome.exit_code == 1
    assert "knowledge_store_unavailable" in outcome.stderr
    assert "/private/" not in outcome.stderr

    def broken_text(*args: object, **kwargs: object) -> None:
        raise PostgresError("secret path /private/documents")

    monkeypatch.setattr(documents, "document_text", broken_text)
    outcome = asyncio.run(
        DocumentReadTool(documents).execute({"document_id": document_id})
    )
    assert outcome.exit_code == 1
    assert "knowledge_store_unavailable" in outcome.stderr
    assert "/private/" not in outcome.stderr
    assert outcome.stdout == ""


def test_unavailable_store_is_distinct_from_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KnowledgeStore(database_for(tmp_path))
    clip, _ = store.create("needle")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise PostgresError("secret path /private/knowledge.sqlite3")

    for tool, args, method in (
        (KnowledgeSearchTool(store), {"query": "needle"}, "list_clips"),
        (KnowledgeListTool(store), {}, "list_clips"),
        (KnowledgeGetTool(store), {"clip_id": clip.clip_id}, "get"),
    ):
        monkeypatch.setattr(store, method, unavailable)
        outcome = asyncio.run(tool.execute(args))
        assert outcome.exit_code == 1
        assert "knowledge_store_unavailable" in outcome.stderr
        assert "/private/" not in outcome.stderr
        assert outcome.stdout == ""


def test_document_tool_can_follow_a_cursor_past_one_million_chars(
    tmp_path: Path,
) -> None:
    store = DocumentStore(database_for(tmp_path), tmp_path / "library")
    text = "字" * 1_000_000 + "🧭尾"
    document, _ = store.create_upload("large.txt", "text/plain", text.encode())
    store.save_extraction(document.document_id, text, "text", "1", [])
    reader = DocumentReadTool(store)

    first = result(
        reader, {"document_id": document.document_id, "offset": 999_999, "max_chars": 2}
    )
    assert first["content"] == "字🧭"
    assert first["next_offset"] == 1_000_001
    last = result(
        reader, {"document_id": document.document_id, "offset": first["next_offset"]}
    )
    assert last["content"] == "尾"
    assert last["next_offset"] is None
