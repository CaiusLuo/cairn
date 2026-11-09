"""Model-aware token counting through LiteLLM's tokenizer resolution.

This lives in the LLM layer so the core context builder stays free of provider
dependencies: ``ContextBuilder`` only sees the ``TokenCounter`` protocol.
"""

from typing import Any

from litellm import token_counter

from cairn.agent.context.window import EstimatedTokenCounter, TokenCount, TokenCounter
from cairn.agent.models.messages import Message
from cairn.agent.models.providers.litellm_client import to_llm_message


class LiteLLMTokenCounter:
    """Count what LiteLLM will send, using the tokenizer it resolves for ``model``.

    LiteLLM picks a tokenizer per model and provider, which is far closer to the
    real request than a byte-size heuristic. When it cannot count -- an unlisted
    model, an unsupported payload or a tokenizer that is not available offline --
    the fallback counter runs and the result stays labelled as an estimate, so an
    approximation is never reported as an exact provider count.
    """

    def __init__(self, model: str, fallback: TokenCounter | None = None) -> None:
        self.model = model
        self.fallback = fallback if fallback is not None else EstimatedTokenCounter()

    def count(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> TokenCount:
        payload = [to_llm_message(message) for message in messages]
        try:
            tokens = self._count_with_litellm(payload, tools)
        except Exception:
            return self.fallback.count(messages, tools)
        # LiteLLM's local tokenizer is useful but it is not a provider-reported
        # usage fact. Keep it labelled as an estimate; no remote count endpoint
        # is called here (Bailian compatibility evidence is insufficient).
        return TokenCount(tokens=tokens, is_estimate=True)

    def _count_with_litellm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        counted = token_counter(
            model=self.model,
            messages=messages,
            tools=tools or None,
        )
        if type(counted) is not int or counted < 0:
            raise ValueError("LiteLLM returned an invalid token count")
        return counted
