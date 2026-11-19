# ruff: noqa: RUF001
"""Best-effort, bounded entity and relation extraction for documents.

One chat-model call per document proposes a small JSON graph. The result is
strictly validated here and kept separate from search: a failed or rejected
graph never blocks lexical or vector retrieval.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from cairn.agent.models.messages import Message
from cairn.agent.models.providers.litellm_client import LiteLLMClient

MAX_ENTITIES = 20
MAX_RELATIONS = 30
MAX_NAME_CHARS = 100
MAX_RELATION_CHARS = 80
ENTITY_KINDS = frozenset(
    {
        "person",
        "organization",
        "project",
        "place",
        "concept",
        "event",
        "product",
        "other",
    }
)

_PROMPT = """你是知识图谱抽取器。输入是带编号的文档片段。
只输出一个 JSON 对象，不要 Markdown 代码围栏，不要解释。
格式：
{"entities":[{"name":"实体名","kind":"person|organization|project|place|concept|event|product|other","chunks":[0,1]}],
 "relations":[{"source":"实体名","target":"实体名","relation":"简短动词短语","chunk":0}]}
规则：
- name 使用片段中的原文名称，不要翻译或改写。
- 只为片段中明确出现的信息建立实体和关系；不确定就省略。
- 最多 20 个实体、30 条关系。
- chunks 与 chunk 使用片段编号；无法确定时可省略 chunk。
"""


class GraphExtractionError(RuntimeError):
    """The provider failed or returned a graph that cannot be used."""


@dataclass(frozen=True, slots=True)
class GraphEntity:
    name: str
    kind: str = "other"
    chunks: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphRelation:
    source: str
    target: str
    relation: str
    chunk: int | None = None


@dataclass(frozen=True, slots=True)
class GraphPayload:
    entities: tuple[GraphEntity, ...] = ()
    relations: tuple[GraphRelation, ...] = ()


class GraphExtractor(Protocol):
    async def extract(self, chunks: Sequence[tuple[int, str]]) -> GraphPayload: ...


class LLMGraphExtractor:
    def __init__(
        self,
        llm: LiteLLMClient,
        *,
        max_chars: int = 12_000,
        max_entities: int = MAX_ENTITIES,
        max_relations: int = MAX_RELATIONS,
    ) -> None:
        self.llm = llm.with_output_limit(1_500)
        self.max_chars = max_chars
        self.max_entities = max_entities
        self.max_relations = max_relations

    async def extract(self, chunks: Sequence[tuple[int, str]]) -> GraphPayload:
        allowed = {ordinal for ordinal, _ in chunks}
        body: list[str] = []
        used = 0
        for ordinal, text in chunks:
            if used >= self.max_chars:
                break
            piece = text[: self.max_chars - used]
            body.append(f"[{ordinal}] {piece}")
            used += len(piece)
        if not body:
            return GraphPayload()
        response = await self.llm.generate(
            [Message(role="user", content=_PROMPT + "\n\n" + "\n\n".join(body))]
        )
        return parse_graph_payload(
            response.content or "",
            allowed=allowed,
            max_entities=self.max_entities,
            max_relations=self.max_relations,
        )


def parse_graph_payload(
    content: str,
    *,
    allowed: set[int],
    max_entities: int = MAX_ENTITIES,
    max_relations: int = MAX_RELATIONS,
) -> GraphPayload:
    payload = _json_object(content)
    raw_entities = payload.get("entities", [])
    raw_relations = payload.get("relations", [])
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise GraphExtractionError("graph payload has the wrong shape")

    entities: list[GraphEntity] = []
    seen: set[str] = set()
    for raw in raw_entities[:max_entities]:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"), MAX_NAME_CHARS)
        if not name or not isinstance(raw.get("chunks", []), list):
            continue
        key = _normalized(name)
        if not key or key in seen:
            continue
        kind = raw.get("kind")
        ordinal_list = [
            value
            for value in raw.get("chunks", [])
            if type(value) is int and value in allowed
        ]
        if not ordinal_list:
            # An entity with no resolvable mention cannot be cited.
            continue
        seen.add(key)
        entities.append(
            GraphEntity(
                name=name,
                kind=kind
                if isinstance(kind, str) and kind in ENTITY_KINDS
                else "other",
                chunks=tuple(dict.fromkeys(ordinal_list)),
            )
        )

    known = {_normalized(entity.name) for entity in entities}
    relations: list[GraphRelation] = []
    seen_relations: set[tuple[str, str, str]] = set()
    for raw in raw_relations[:max_relations]:
        if not isinstance(raw, dict):
            continue
        source = _text(raw.get("source"), MAX_NAME_CHARS)
        target = _text(raw.get("target"), MAX_NAME_CHARS)
        relation = _text(raw.get("relation"), MAX_RELATION_CHARS)
        if (
            not source
            or not target
            or not relation
            or _normalized(source) not in known
            or _normalized(target) not in known
            or _normalized(source) == _normalized(target)
        ):
            continue
        relation_key = (
            _normalized(source),
            _normalized(target),
            relation.casefold(),
        )
        if relation_key in seen_relations:
            continue
        seen_relations.add(relation_key)
        chunk = raw.get("chunk")
        relations.append(
            GraphRelation(
                source=source,
                target=target,
                relation=relation,
                chunk=chunk if type(chunk) is int and chunk in allowed else None,
            )
        )
    return GraphPayload(entities=tuple(entities), relations=tuple(relations))


def _json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise GraphExtractionError("graph response contained no JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise GraphExtractionError("graph response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise GraphExtractionError("graph response was not a JSON object")
    return payload


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _normalized(name: str) -> str:
    return " ".join(name.casefold().split())


__all__ = [
    "ENTITY_KINDS",
    "GraphEntity",
    "GraphExtractionError",
    "GraphExtractor",
    "GraphPayload",
    "GraphRelation",
    "LLMGraphExtractor",
    "parse_graph_payload",
]
