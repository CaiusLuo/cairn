"""Shared doubles for compaction tests: message builders, a counter, a summarizer.

Compaction is measured in tokens, so its tests need a counter whose arithmetic a
reader can check by hand. Every such test used to declare its own copy; one
definition keeps them comparable. The message builders exist for the same reason:
cut points, grouping and substitution are all defined on message *shape*, so each
test has to build the same shapes, and one definition keeps those shapes equal.
"""

from typing import Any

from cairn.core.compaction.summarizer import SummaryResult
from cairn.core.context import TokenCount
from cairn.core.models import Message, ToolCall


class CharacterCounter:
    """One token per character, plus one per message. No tokenizer involved."""

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            tokens=sum(len(message.content or "") for message in messages),
            is_estimate=False,
        )


class StubSummarizer:
    """A summarizer that records its calls and returns a fixed outcome.

    ``error`` raises from the call, which is how an adapter failure arrives;
    ``failure`` returns a failure *value*, which is how the real adapter reports
    an empty or overlong summary. The two paths are distinct in the contract and a
    double must be able to produce either.
    """

    def __init__(
        self,
        summary: str = "summary",
        error: BaseException | None = None,
        failure: str | None = None,
    ) -> None:
        self.summary = summary
        self.error = error
        self.failure = failure
        self.calls: list[tuple[list[Message], int]] = []

    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        self.calls.append((messages, max_tokens))
        if self.error is not None:
            raise self.error
        if self.failure is not None:
            return SummaryResult.failed(self.failure)
        return SummaryResult(text=self.summary)


def user(content: str = "u") -> Message:
    return Message(role="user", content=content)


def assistant(content: str = "a") -> Message:
    return Message(role="assistant", content=content)


def tool_call(
    call_id: str,
    *,
    name: str = "read_file",
    arguments: dict[str, Any] | None = None,
) -> Message:
    return Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments={"path": "a"} if arguments is None else arguments,
            )
        ],
    )


def tool_result(call_id: str, *, content: str = "x") -> Message:
    return Message(role="tool", tool_call_id=call_id, content=content)


def group(call_id: str, *, result_content: str = "x") -> list[Message]:
    """One complete tool interaction: the call and its result."""
    return [tool_call(call_id), tool_result(call_id, content=result_content)]
