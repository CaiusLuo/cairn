"""Resolve durable chat references against the current private library."""

from typing import Any

from psycopg import Error as PostgresError
from pydantic import ValidationError

from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.documents import MAX_KNOWLEDGE_SOURCES, DocumentReference


def label_knowledge_sources(
    messages: list[dict[str, Any]], documents: DocumentStore
) -> None:
    cards: list[tuple[dict[str, Any], str, list[DocumentReference]]] = []
    for message in messages:
        card = message.pop("knowledge", None)
        if card is None:
            continue
        references = []
        for value in card["references"][:MAX_KNOWLEDGE_SOURCES]:
            try:
                references.append(DocumentReference.model_validate(value))
            except ValidationError:
                continue
        if references:
            cards.append((message, card["run_id"], references))

    try:
        labels = documents.document_source_labels(
            list(dict.fromkeys(ref for _, _, refs in cards for ref in refs))
        )
    except (OSError, PostgresError):
        labels = {}

    for message, run_id, references in cards:
        message["knowledge"] = {
            "run_id": run_id,
            "sources": [
                {
                    "document_id": ref.document_id,
                    "chunk_id": ref.chunk_id,
                    **labels.get(ref, {"title": None, "heading": ""}),
                }
                for ref in references
            ],
        }
