"""Owner-only read tools must read real persisted clips, never runtime files."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from cairn.agent.tools.base import InvalidArguments, Tool
from cairn.knowledge.store import KnowledgeStore
from cairn.knowledge.tools import (
    KnowledgeGetTool,
    KnowledgeListTool,
    KnowledgeSearchTool,
)


def result(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    outcome = asyncio.run(tool.execute(args))
    assert outcome.exit_code == 0, outcome.stderr
    return cast(dict[str, Any], json.loads(outcome.stdout))


def test_list_search_get_pagination_and_archived_filter(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
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

    reopened = KnowledgeStore(tmp_path / "knowledge")
    assert (
        result(KnowledgeGetTool(reopened), {"clip_id": first.clip_id})["status"] == "ok"
    )


def test_chinese_keyword_search_returns_citeable_clip(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    clip, _ = store.create("项目代号: 蓝石。下周与供应商确认交付时间。")
    outcome = result(KnowledgeSearchTool(store), {"query": "蓝石"})
    assert outcome["status"] == "ok"
    assert [item["clip_id"] for item in outcome["clips"]] == [clip.clip_id]
    assert "蓝石" in outcome["clips"][0]["preview"]


def test_tools_validate_model_arguments(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    for tool, args in (
        (KnowledgeListTool(store), {"limit": True}),
        (KnowledgeListTool(store), {"limit": 0}),
        (KnowledgeListTool(store), {"limit": 21}),
        (KnowledgeListTool(store), {"offset": -1}),
        (KnowledgeListTool(store), {"offset": 1_000_001}),
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


def test_unavailable_store_is_distinct_from_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    clip, _ = store.create("needle")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("secret path /private/knowledge.sqlite3")

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
