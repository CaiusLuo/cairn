"""Shared doubles for compaction tests: a deterministic counter and a dispatcher.

Compaction is measured in tokens, so its tests need a counter whose arithmetic a
reader can check by hand. Every such test used to declare its own copy; one
definition keeps them comparable.
"""

from typing import Any

from cairn.core.compaction.summarizer import SummaryResult
from cairn.core.context import TokenCount
from cairn.core.models import Message


class CharacterCounter:
    """One token per character, plus one per message. No tokenizer involved."""

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            tokens=sum(len(message.content or "") for message in messages),
            is_estimate=False,
        )


class StubSummarizer:
    """A summarizer that records its calls and returns a fixed summary."""

    def __init__(
        self, summary: str = "summary", error: BaseException | None = None
    ) -> None:
        self.summary = summary
        self.error = error
        self.calls: list[tuple[list[Message], int]] = []

    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        self.calls.append((messages, max_tokens))
        if self.error is not None:
            raise self.error
        return SummaryResult(text=self.summary)
