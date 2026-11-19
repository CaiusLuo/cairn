"""Embedding client behaviour without any provider call."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from cairn.knowledge.embeddings import (
    EmbeddingError,
    LiteLLMEmbeddingClient,
    litellm_model,
    parse_vectors,
)
from tests.support.knowledge_fakes import FailingEmbedder, FakeEmbedder


def test_bare_model_ids_are_routed_through_openai_compatible_client() -> None:
    assert (
        litellm_model("qwen3.7-text-embedding-flash")
        == "openai/qwen3.7-text-embedding-flash"
    )
    assert litellm_model("openai/custom") == "openai/custom"


def test_parse_vectors_accepts_objects_and_dicts() -> None:
    response = SimpleNamespace(
        data=[
            SimpleNamespace(embedding=[1, 2.5, 3]),
            SimpleNamespace(embedding=[4.0, 5.0, 6.0]),
        ]
    )
    assert parse_vectors(response, expected=2) == [
        [1.0, 2.5, 3.0],
        [4.0, 5.0, 6.0],
    ]
    assert parse_vectors({"data": [{"embedding": [0.5, 0.5]}]}, expected=1, dims=2) == [
        [0.5, 0.5]
    ]


@pytest.mark.parametrize(
    "response, expected, dims",
    [
        (SimpleNamespace(data=[]), 1, 0),
        (SimpleNamespace(data=[SimpleNamespace(embedding=[])]), 1, 0),
        (SimpleNamespace(data=[SimpleNamespace(embedding=[1, 2])]), 1, 3),
        (
            SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1, 2]),
                    SimpleNamespace(embedding=[1, 2, 3]),
                ]
            ),
            2,
            0,
        ),
        (SimpleNamespace(data=[SimpleNamespace(embedding=[1, float("inf")])]), 1, 0),
        (SimpleNamespace(data=[SimpleNamespace(embedding=["x"])]), 1, 0),
    ],
)
def test_parse_vectors_rejects_unusable_responses(
    response: object, expected: int, dims: int
) -> None:
    with pytest.raises(EmbeddingError):
        parse_vectors(response, expected=expected, dims=dims)


def test_client_returns_vectors_and_sends_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_aembedding(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[0.1] * 4),
                SimpleNamespace(embedding=[0.2] * 4),
            ]
        )

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    client = LiteLLMEmbeddingClient(
        "qwen3.7-text-embedding-flash",
        api_key="test-key",
        api_base="https://example.invalid/v1",
        dims=4,
    )
    vectors = asyncio.run(client.embed(["a", "b"]))

    assert len(vectors) == 2
    assert captured["model"] == "openai/qwen3.7-text-embedding-flash"
    assert captured["dimensions"] == 4
    assert captured["max_retries"] == 0


def test_client_error_never_echoes_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "sk-test-secret-value"

    async def failing_aembedding(**kwargs: object) -> object:
        raise RuntimeError(f"401 unauthorized: {key} " + "x" * 1000)

    monkeypatch.setattr(litellm, "aembedding", failing_aembedding)
    client = LiteLLMEmbeddingClient("model", api_key=key)
    with pytest.raises(EmbeddingError) as caught:
        asyncio.run(client.embed(["hello"]))

    message = str(caught.value)
    assert key not in message
    assert "[redacted]" in message
    assert len(message) <= len("embedding request failed: ") + 301


def test_client_without_a_key_fails_before_calling_the_provider() -> None:
    client = LiteLLMEmbeddingClient("model", api_key=None)
    with pytest.raises(EmbeddingError, match="credential"):
        asyncio.run(client.embed(["hello"]))


def test_empty_batch_returns_empty_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(**kwargs: object) -> object:
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(litellm, "aembedding", unexpected)
    client = LiteLLMEmbeddingClient("model", api_key="key")
    assert asyncio.run(client.embed([])) == []


def test_fake_embedder_is_deterministic_and_semantic() -> None:
    fake = FakeEmbedder(dims=16)

    async def run() -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
        first = await fake.embed(["部署服务器", "服务器部署"])
        second = await fake.embed(["部署服务器", "完全无关的词语"])
        again = await fake.embed(["部署服务器"])
        return first, second, again

    first, second, again = asyncio.run(run())
    assert first[0] == again[0]

    def cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert cosine(first[0], first[1]) > cosine(second[0], second[1])


def test_failing_embedder_is_explicit() -> None:
    with pytest.raises(EmbeddingError, match="provider is down"):
        asyncio.run(FailingEmbedder().embed(["x"]))
