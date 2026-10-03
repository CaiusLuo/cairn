"""Build bounded request views without removing the original conversation facts."""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from cairn.core.models import Message


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_tokens: int = 32768
    response_tokens: int = 4096

    def __post_init__(self) -> None:
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(self.response_tokens) is not int or self.response_tokens < 1:
            raise ValueError("response_tokens must be a positive integer")
        if self.response_tokens >= self.max_tokens:
            raise ValueError("response_tokens must be smaller than max_tokens")


@dataclass(frozen=True, slots=True)
class TokenCount:
    tokens: int
    is_estimate: bool

    def __post_init__(self) -> None:
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("tokens must be a nonnegative integer")
        if type(self.is_estimate) is not bool:
            raise ValueError("is_estimate must be a boolean")


class TokenCounter(Protocol):
    def count(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> TokenCount: ...


class EstimatedTokenCounter:
    """Offline size estimate; this is not a model tokenizer or provider usage.

    Include serialized fields, JSON arguments, schemas and framing overhead.
    Tokenization varies by model, so callers needing an exact bound should inject
    a model-aware counter instead.
    """

    def count(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> TokenCount:
        serialized = json.dumps(
            {
                "messages": [message.model_dump() for message in messages],
                "tools": tools,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        byte_count = len(serialized.encode("utf-8"))
        tokens = (byte_count + 3) // 4 + 8 * len(messages) + 16 * len(tools) + 3
        return TokenCount(tokens=tokens, is_estimate=True)


@dataclass(frozen=True, slots=True)
class ContextRequest:
    messages: list[Message]
    tokens_before: TokenCount
    tokens_after: TokenCount
    omitted_turns: int
    omitted_messages: int


class ContextBudgetExceeded(Exception):
    def __init__(self, *, tokens: TokenCount, budget: ContextBudget) -> None:
        self.input_tokens = tokens.tokens
        self.response_tokens = budget.response_tokens
        self.max_tokens = budget.max_tokens
        self.is_estimate = tokens.is_estimate
        mode = "estimated" if self.is_estimate else "exact"
        super().__init__(
            "Required context exceeds the context budget: "
            f"{mode} input {self.input_tokens} + response reserve "
            f"{self.response_tokens} > limit {self.max_tokens}. "
            "The current turn and tool results cannot be trimmed."
        )


def _validate_history(history: list[Message], current_turn_start: int) -> None:
    if (
        type(current_turn_start) is not int
        or not 0 <= current_turn_start < len(history)
        or history[current_turn_start].role != "user"
    ):
        raise ValueError("current_turn_start must identify the current user message")
    if history[0].role != "user":
        raise ValueError("Conversation history must start with a user message")
    if any(message.role == "user" for message in history[current_turn_start + 1 :]):
        raise ValueError("The current turn cannot contain another user message")

    pending: set[str] = set()
    for message in history:
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise ValueError("Conversation history contains an orphan tool result")
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ValueError(
                    "Conversation history contains an incomplete tool group"
                )
            if message.tool_calls:
                if message.role != "assistant":
                    raise ValueError("Only assistant messages may contain tool calls")
                pending = {call.id for call in message.tool_calls}
                if len(pending) != len(message.tool_calls):
                    raise ValueError("A tool-call group contains duplicate call IDs")
    if pending:
        raise ValueError("Conversation history contains an incomplete tool group")


class ContextBuilder:
    def __init__(
        self,
        budget: ContextBudget | None = None,
        counter: TokenCounter | None = None,
    ) -> None:
        self.budget = budget if budget is not None else ContextBudget()
        self.counter = counter if counter is not None else EstimatedTokenCounter()

    def _count(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> TokenCount:
        count = self.counter.count(messages, tools)
        # Keep injected counters subject to the same validity contract.
        if not isinstance(count, TokenCount):
            raise ValueError("Token counter must return a TokenCount")
        if type(count.tokens) is not int or count.tokens < 0:
            raise ValueError("Token counter returned an invalid token count")
        if type(count.is_estimate) is not bool:
            raise ValueError("Token counter returned an invalid estimate marker")
        return count

    def _fits(self, count: TokenCount) -> bool:
        return count.tokens + self.budget.response_tokens <= self.budget.max_tokens

    def build(
        self,
        *,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
    ) -> ContextRequest:
        _validate_history(history, current_turn_start)
        messages = [*system_messages, *history]
        tokens_before = self._count(messages, tools)
        if self._fits(tokens_before):
            return ContextRequest(messages, tokens_before, tokens_before, 0, 0)

        # Each next user message closes the preceding complete old turn. This
        # also includes recovered turns ending in tool results rather than text.
        old_turn_ends = [
            index
            for index in range(1, current_turn_start + 1)
            if history[index].role == "user"
        ]
        tokens_after = tokens_before
        for omitted_turns, omitted_messages in enumerate(old_turn_ends, start=1):
            notice = Message(
                role="system",
                content=(
                    f"Context budget: omitted {omitted_turns} earlier complete "
                    f"turn(s) ({omitted_messages} messages). The current turn is "
                    "preserved. Omitted history is unavailable in this request; "
                    "do not assume earlier tool actions did not happen."
                ),
            )
            messages = [*system_messages, notice, *history[omitted_messages:]]
            tokens_after = self._count(messages, tools)
            if self._fits(tokens_after):
                return ContextRequest(
                    messages,
                    tokens_before,
                    tokens_after,
                    omitted_turns,
                    omitted_messages,
                )

        raise ContextBudgetExceeded(tokens=tokens_after, budget=self.budget)
