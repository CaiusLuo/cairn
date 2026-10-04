"""Build bounded request views without removing the original conversation facts."""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from cairn.core.models import Message

#: Marker used by tests and callers to recognise the omission notice.
OMISSION_NOTICE_PREFIX = "Context notice:"


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
    ) -> TokenCount:
        """Count what this request would cost.

        ``is_estimate`` must be True unless the result comes from a real
        tokenizer. Implementations must be monotone: appending messages or tools
        never lowers the count. ContextBuilder relies on that contract to probe
        omission candidates with a logarithmic number of counts, and it only
        returns a view whose fit it has measured.
        """
        ...


class EstimatedTokenCounter:
    """Offline size estimate; this is not a model tokenizer or provider usage.

    Include serialized fields, JSON arguments, schemas and framing overhead.
    Tokenization varies by model, so callers needing a provider bound should
    inject a model-aware counter instead. Measured against a tokenizer this
    heuristic overestimates prose but underestimates code, hexadecimal and
    base64-like content, which is common in tool output.
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


class ConversationHistoryError(ValueError):
    """History cannot form a provider-valid request even after trimming.

    Raised when the current turn itself holds an orphan tool result, an
    incomplete tool-call group or duplicate call ids. Broken *older* turns are
    omitted from the request view instead, so one malformed provider response
    cannot make every later turn fail.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"Conversation history cannot form a valid request: {reason}. "
            "The current turn is never trimmed, so the request cannot be "
            "repaired; start a new session."
        )


class ContextBudgetExceeded(Exception):
    def __init__(
        self,
        *,
        tokens: TokenCount,
        budget: ContextBudget,
        components: dict[str, int] | None = None,
    ) -> None:
        self.input_tokens = tokens.tokens
        self.response_tokens = budget.response_tokens
        self.max_tokens = budget.max_tokens
        self.is_estimate = tokens.is_estimate
        self.components = dict(components or {})
        mode = "estimated" if self.is_estimate else "exact"
        detail = ""
        if self.components:
            breakdown = ", ".join(
                f"{name} {count}" for name, count in self.components.items()
            )
            detail = f" Never-trimmed content: {breakdown}."
        super().__init__(
            "Required context exceeds the context budget: "
            f"{mode} input {self.input_tokens} + response reserve "
            f"{self.response_tokens} > limit {self.max_tokens}.{detail} "
            "The current turn and tool results cannot be trimmed; raise "
            "CAIRN_CONTEXT_MAX_TOKENS or lower CAIRN_RESPONSE_MAX_TOKENS."
        )


def _validate_turn_boundaries(history: list[Message], current_turn_start: int) -> None:
    """Check the structure trimming depends on.

    These are internal-invariant failures: without user-message turn boundaries
    the builder cannot describe which whole turns it omitted.
    """
    if (
        type(current_turn_start) is not int
        or not 0 <= current_turn_start < len(history)
        or history[current_turn_start].role != "user"
    ):
        raise ValueError("current_turn_start must identify the current user message")
    if history[0].role != "user":
        raise ValueError("Conversation history must start with a user message")
    if any(
        history[index].role == "user"
        for index in range(current_turn_start + 1, len(history))
    ):
        raise ValueError("The current turn cannot contain another user message")


def _tool_group_error(messages: list[Message]) -> str | None:
    """Return why a provider would reject these tool calls/results, if any."""
    pending: set[str] = set()

    for message in messages:
        if message.role == "tool":
            if message.tool_call_id not in pending:
                return "a tool result has no matching assistant tool call"
            pending.remove(message.tool_call_id)
            continue

        if pending:
            return "an assistant tool call has no matching tool result"
        if message.tool_calls:
            if message.role != "assistant":
                return "only assistant messages may contain tool calls"
            call_ids = {call.id for call in message.tool_calls}
            if len(call_ids) != len(message.tool_calls):
                return "a tool-call group contains duplicate call ids"
            pending = call_ids

    if pending:
        return "an assistant tool call has no matching tool result"
    return None


def _omission_notice(omitted_turns: int, omitted_messages: int) -> Message:
    return Message(
        role="system",
        content=(
            f"{OMISSION_NOTICE_PREFIX} omitted {omitted_turns} earlier complete "
            f"turn(s) ({omitted_messages} messages) from this request. The current "
            "turn is preserved. Omitted history is unavailable in this request; "
            "do not assume earlier tool actions did not happen."
        ),
    )


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

    def _components(
        self,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Describe the never-trimmed content for an overflow error."""
        components: dict[str, int] = {}
        if system_messages:
            components["system/repository context"] = self._count(
                system_messages, []
            ).tokens
        if tools:
            components["tool schemas"] = self._count([], tools).tokens
        # Turn boundaries are validated, so the current turn is never empty.
        components["current turn"] = self._count(
            history[current_turn_start:], []
        ).tokens
        return components

    def build(
        self,
        *,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
    ) -> ContextRequest:
        _validate_turn_boundaries(history, current_turn_start)

        full_view = [*system_messages, *history]
        tokens_before = self._count(full_view, tools)
        full_history_error = _tool_group_error(history)
        if full_history_error is None and self._fits(tokens_before):
            return ContextRequest(full_view, tokens_before, tokens_before, 0, 0)

        # Each next user message closes the preceding complete old turn. This
        # also includes recovered turns ending in tool results rather than text.
        old_turn_ends = [
            index
            for index in range(1, current_turn_start + 1)
            if history[index].role == "user"
        ]
        if not old_turn_ends:
            if full_history_error is not None:
                raise ConversationHistoryError(full_history_error)
            raise ContextBudgetExceeded(
                tokens=tokens_before,
                budget=self.budget,
                components=self._components(
                    system_messages, history, current_turn_start, tools
                ),
            )

        views: dict[int, tuple[list[Message], TokenCount]] = {}

        def retained_for(omitted_turns: int) -> list[Message]:
            return history[old_turn_ends[omitted_turns - 1] :]

        def view_for(omitted_turns: int) -> tuple[list[Message], TokenCount]:
            cached = views.get(omitted_turns)
            if cached is None:
                omitted_messages = old_turn_ends[omitted_turns - 1]
                view = [
                    *system_messages,
                    _omission_notice(omitted_turns, omitted_messages),
                    *history[omitted_messages:],
                ]
                cached = (view, self._count(view, tools))
                views[omitted_turns] = cached
            return cached

        def acceptable(omitted_turns: int) -> bool:
            # Valid full history guarantees that each whole-turn suffix is valid.
            # Recheck only when trimming must repair malformed history.
            if (
                full_history_error is not None
                and _tool_group_error(retained_for(omitted_turns)) is not None
            ):
                return False
            return self._fits(view_for(omitted_turns)[1])

        max_omitted = len(old_turn_ends)

        # The current turn is never trimmed, so a broken group inside it is not
        # repairable here.
        if full_history_error is not None:
            current_turn_error = _tool_group_error(retained_for(max_omitted))
            if current_turn_error is not None:
                raise ConversationHistoryError(current_turn_error)

        minimal_tokens = view_for(max_omitted)[1]
        if not self._fits(minimal_tokens):
            raise ContextBudgetExceeded(
                tokens=minimal_tokens,
                budget=self.budget,
                components=self._components(
                    system_messages, history, current_turn_start, tools
                ),
            )

        # acceptable() only becomes true as turns are dropped: dropping a whole
        # old turn cannot break a retained group nor raise the count for a
        # counter honouring the TokenCounter contract. The search therefore
        # finds the smallest omission set that both fits the budget and keeps
        # the retained tool groups valid, and it only moves `high` onto a
        # candidate whose fit was measured, so the returned view is verified.
        low, high = 1, max_omitted
        while low < high:
            middle = (low + high) // 2
            if acceptable(middle):
                high = middle
            else:
                low = middle + 1

        view, tokens_after = view_for(low)
        return ContextRequest(
            view,
            tokens_before,
            tokens_after,
            low,
            old_turn_ends[low - 1],
        )
