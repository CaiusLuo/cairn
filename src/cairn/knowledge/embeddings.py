"""Embedding client for the private document library.

Calls an OpenAI-compatible embeddings endpoint (Bailian/DashScope by default)
with a per-call credential. Errors are redacted and bounded before they reach
durable document state, so a provider that echoes a key cannot leak it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Protocol

from cairn.shared.redaction import redact_text

MAX_ERROR_CHARS = 300
EMBEDDING_TIMEOUT_SECONDS = 15
DEFAULT_EMBEDDING_BATCH_SIZE = 10
MAX_VECTOR_DIMS = 16_000
_MAX_FLOAT32 = 3.4028234663852886e38
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class EmbeddingError(RuntimeError):
    """An embedding request failed or returned an unusable response."""


class EmbeddingClient(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def litellm_model(model: str) -> str:
    """Treat provider-style model IDs as names on the configured endpoint."""
    return model if model.startswith("openai/") else "openai/" + model


class LiteLLMEmbeddingClient:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None,
        api_base: str = DEFAULT_EMBEDDING_BASE_URL,
        dims: int = 0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.dims = dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_base:
            raise EmbeddingError("embedding endpoint is not configured")
        if not self.api_key:
            raise EmbeddingError(
                "embedding credential is not configured for this endpoint"
            )
        import litellm

        options: dict[str, Any] = {}
        if self.dims:
            options["dimensions"] = self.dims
        try:
            response = await litellm.aembedding(
                model=litellm_model(self.model),
                input=list(texts),
                api_key=self.api_key,
                api_base=self.api_base,
                max_retries=0,
                timeout=EMBEDDING_TIMEOUT_SECONDS,
                **options,
            )
        except Exception as error:
            raise EmbeddingError(self._safe_message(error)) from None
        return parse_vectors(response, expected=len(texts), dims=self.dims)

    def _safe_message(self, error: Exception) -> str:
        message = redact_text(str(error), (self.api_key or "",)).strip()
        if not message:
            message = "no provider detail"
        if len(message) > MAX_ERROR_CHARS:
            message = message[:MAX_ERROR_CHARS] + "…"
        return f"embedding request failed: {message}"


def parse_vectors(
    response: object, *, expected: int, dims: int = 0
) -> list[list[float]]:
    """Validate an EmbeddingResponse: one finite, uniform vector per input."""
    data = (
        response.get("data")
        if isinstance(response, dict)
        else getattr(response, "data", None)
    )
    if not isinstance(data, (list, tuple)) or len(data) != expected:
        raise EmbeddingError("embedding response did not match the request")
    vectors: list[list[float]] = []
    indexes: list[object] = []
    size: int | None = None
    for item in data:
        indexes.append(
            item.get("index")
            if isinstance(item, dict)
            else getattr(item, "index", None)
        )
        raw = (
            item.get("embedding")
            if isinstance(item, dict)
            else getattr(item, "embedding", None)
        )
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_VECTOR_DIMS:
            raise EmbeddingError("embedding response contained no vector")
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError, OverflowError):
            raise EmbeddingError(
                "embedding response contained a non-numeric value"
            ) from None
        if any(
            not math.isfinite(value) or abs(value) > _MAX_FLOAT32 for value in vector
        ):
            raise EmbeddingError("embedding response contained a non-finite value")
        if size is None:
            size = len(vector)
            if dims and size != dims:
                raise EmbeddingError(
                    f"embedding response had {size} dimensions, expected {dims}"
                )
        elif len(vector) != size:
            raise EmbeddingError("embedding response mixed vector dimensions")
        vectors.append(vector)
    if all(index is None for index in indexes):
        return vectors
    indexed: dict[int, list[float]] = {}
    for index, vector in zip(indexes, vectors, strict=True):
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < expected
            or index in indexed
        ):
            raise EmbeddingError("embedding response contained an invalid input index")
        indexed[index] = vector
    return [indexed[index] for index in range(expected)]


__all__ = [
    "DEFAULT_EMBEDDING_BASE_URL",
    "EmbeddingClient",
    "EmbeddingError",
    "LiteLLMEmbeddingClient",
    "litellm_model",
    "parse_vectors",
]
