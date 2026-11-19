"""Strict, bounded parsing of LLM graph extraction output."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from cairn.agent.models.messages import LLMResponse, Message
from cairn.knowledge.graph import (
    GraphExtractionError,
    LLMGraphExtractor,
    parse_graph_payload,
)


def test_graph_payload_is_validated_and_deduplicated() -> None:
    content = json.dumps(
        {
            "entities": [
                {"name": "Cairn", "kind": "project", "chunks": [0, 1]},
                {"name": " cairn ", "kind": "project", "chunks": [1]},
                {"name": "PostgreSQL", "kind": "database", "chunks": [0]},
                {"name": "No mention", "kind": "concept", "chunks": [9]},
            ],
            "relations": [
                {
                    "source": "Cairn",
                    "target": "PostgreSQL",
                    "relation": "uses",
                    "chunk": 0,
                },
                {"source": "Cairn", "target": "Ghost", "relation": "uses", "chunk": 0},
                {"source": "Cairn", "target": "Cairn", "relation": "is", "chunk": 0},
            ],
        }
    )
    payload = parse_graph_payload(content, allowed={0, 1})

    assert [(entity.name, entity.kind) for entity in payload.entities] == [
        ("Cairn", "project"),
        ("PostgreSQL", "other"),
    ]
    assert payload.entities[0].chunks == (0, 1)
    assert len(payload.relations) == 1
    relation = payload.relations[0]
    assert (relation.source, relation.target, relation.relation) == (
        "Cairn",
        "PostgreSQL",
        "uses",
    )
    assert relation.chunk == 0


def test_fenced_json_is_accepted() -> None:
    content = '```json\n{"entities":[{"name":"甲","kind":"other","chunks":[0]}]}\n```'
    payload = parse_graph_payload(content, allowed={0})
    assert [entity.name for entity in payload.entities] == ["甲"]


def test_entities_keep_only_their_own_kind_strings() -> None:
    content = json.dumps(
        {
            "entities": [
                {"name": "bad kind", "kind": ["not", "a", "string"], "chunks": [0]}
            ]
        }
    )
    payload = parse_graph_payload(content, allowed={0})
    assert payload.entities[0].kind == "other"


def test_entity_count_is_bounded() -> None:
    entities = [{"name": f"实体{i}", "kind": "other", "chunks": [0]} for i in range(50)]
    payload = parse_graph_payload(
        json.dumps({"entities": entities}), allowed={0}, max_entities=5
    )
    assert len(payload.entities) == 5


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json at all",
        '{"entities": {}}',
        "[1, 2, 3]",
    ],
)
def test_unusable_payloads_raise(content: str) -> None:
    with pytest.raises(GraphExtractionError):
        parse_graph_payload(content, allowed={0})


def test_non_object_entries_are_ignored() -> None:
    payload = parse_graph_payload('{"entities": [1, 2]}', allowed={0})
    assert payload.entities == ()
    assert payload.relations == ()


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[Message] = []
        self.limit: int | None = None

    def with_output_limit(self, limit: int) -> _FakeLLM:
        self.limit = limit
        return self

    async def generate(self, messages: list[Message]) -> LLMResponse:
        self.messages = messages
        return LLMResponse(content=self.content)


def test_llm_extractor_sends_chunks_and_parses_the_answer() -> None:
    llm = _FakeLLM(
        json.dumps(
            {
                "entities": [
                    {"name": "Cairn", "kind": "project", "chunks": [0]},
                    {"name": "PostgreSQL", "kind": "concept", "chunks": [0]},
                ],
                "relations": [
                    {
                        "source": "Cairn",
                        "target": "PostgreSQL",
                        "relation": "uses",
                        "chunk": 0,
                    }
                ],
            }
        )
    )
    extractor = LLMGraphExtractor(cast(Any, llm))
    payload = asyncio.run(extractor.extract([(0, "Cairn 使用 PostgreSQL 存储知识。")]))

    assert llm.limit == 1_500
    assert len(llm.messages) == 1
    prompt = llm.messages[0].content or ""
    assert "Cairn 使用 PostgreSQL" in prompt
    assert [entity.name for entity in payload.entities] == ["Cairn", "PostgreSQL"]


class _PlainLLM:
    """An offline model without with_output_limit, as in provider-free runs."""

    async def generate(self, messages: list[Message]) -> LLMResponse:
        return LLMResponse(content='{"entities":[{"name":"甲","chunks":[0]}]}')


def test_llm_extractor_accepts_a_model_without_output_limits() -> None:
    extractor = LLMGraphExtractor(cast(Any, _PlainLLM()))

    payload = asyncio.run(extractor.extract([(0, "甲 是测试实体")]))

    assert [entity.name for entity in payload.entities] == ["甲"]


def test_llm_extractor_returns_nothing_for_no_chunks() -> None:
    llm = _FakeLLM("{}")
    extractor = LLMGraphExtractor(cast(Any, llm))
    payload = asyncio.run(extractor.extract([]))
    assert payload.entities == ()
    assert llm.messages == []
