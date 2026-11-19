"""Deterministic offline knowledge clients for tests.

Tests must never call a paid provider. The fake embedder derives a vector from
token hashes, so texts sharing words or characters are genuinely similar under
cosine distance and retrieval tests can assert ranking rather than ids.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from cairn.knowledge.embeddings import EmbeddingError
from cairn.knowledge.graph import GraphExtractionError, GraphPayload

_TOKENS = re.compile(r"[A-Za-z0-9]+|[㐀-䶿一-鿿]")


class FakeEmbedder:
    def __init__(self, *, model: str = "fake-embedding", dims: int = 8) -> None:
        self.model = model
        self.dims = dims
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        return [self._vector(text) for text in batch]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        tokens = _TOKENS.findall(text.lower())
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dims] += 1.0
        if not any(vector):
            vector[0] = 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


class FailingEmbedder:
    def __init__(
        self, *, model: str = "fake-embedding", message: str = "provider is down"
    ) -> None:
        self.model = model
        self.message = message
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingError(self.message)


class FakeGraphExtractor:
    def __init__(self, payload: GraphPayload | None = None) -> None:
        self.payload = payload or GraphPayload()
        self.calls: list[list[tuple[int, str]]] = []

    async def extract(self, chunks: Sequence[tuple[int, str]]) -> GraphPayload:
        self.calls.append(list(chunks))
        return self.payload


class FailingGraphExtractor:
    def __init__(self, message: str = "graph provider is down") -> None:
        self.message = message
        self.calls = 0

    async def extract(self, chunks: Sequence[tuple[int, str]]) -> GraphPayload:
        self.calls += 1
        raise GraphExtractionError(self.message)
