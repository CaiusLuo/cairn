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
    assert result(KnowledgeSearchTool(store), {"query": "absent"})["status"] == "empty"

    detail = result(KnowledgeGetTool(store), {"clip_id": first.clip_id})
    assert detail["clip"]["clip_id"] == first.clip_id
    assert len(detail["content"]) == 4000
    assert detail["next_offset"] == 4000
    rest = result(
        KnowledgeGetTool(store),
        {"clip_id": first.clip_id, "offset": detail["next_offset"], "max_chars": 1000},
    )
    assert len(rest["content"]) == 1000
    assert detail["untrusted_reference"] is True

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


def test_tools_validate_model_arguments(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    for tool, args in (
        (KnowledgeListTool(store), {"limit": True}),
        (KnowledgeListTool(store), {"include_archived": True}),
        (KnowledgeSearchTool(store), {"query": " "}),
        (KnowledgeSearchTool(store), {"query": "x" * 501}),
        (KnowledgeGetTool(store), {"clip_id": ""}),
        (KnowledgeGetTool(store), {"clip_id": "clip-1", "max_chars": 5000}),
    ):
        with pytest.raises(InvalidArguments):
            tool.validate(args)


def test_unavailable_store_is_distinct_from_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("secret path /private/knowledge.sqlite3")

    monkeypatch.setattr(store, "list_clips", unavailable)
    outcome = asyncio.run(KnowledgeSearchTool(store).execute({"query": "needle"}))
    assert outcome.exit_code == 1
    assert "knowledge_store_unavailable" in outcome.stderr
    assert "/private/" not in outcome.stderr
