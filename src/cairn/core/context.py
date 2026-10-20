"""Build bounded request views without removing the original conversation facts."""

import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from cairn.core.compaction import CompactionRecord, apply_replacement
from cairn.core.compaction.budget_values import (
    DEFAULT_CONTEXT_MAX_TOKENS,
    DEFAULT_RESPONSE_MAX_TOKENS,
    ContextBudget,
    TokenCount,
)
from cairn.core.compaction.replacement import folded_placeholder
from cairn.core.memory.context import (
    MEMORY_DATA_POLICY,
    MemoryContext,
    MemoryContextItem,
    render_memory,
)
from cairn.core.models import Message, ToolCall

__all__ = [
    # Budget values now live beside the compaction policy. Re-exported here so
    # the context view keeps one import site for callers that only need budget.
    "DEFAULT_CONTEXT_MAX_TOKENS",
    "DEFAULT_RESPONSE_MAX_TOKENS",
    "OMISSION_NOTICE_PREFIX",
    "CompactionRecord",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ContextBuilder",
    "ContextRequest",
    "ConversationHistoryError",
    "EstimatedTokenCounter",
    "TokenCount",
    "TokenCounter",
]

#: Marker used by tests and callers to recognise the omission notice.
OMISSION_NOTICE_PREFIX = "Context notice:"


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


def _shift(current_turn_start: int, record: CompactionRecord) -> int:
    """Where the current turn lands after the summary replaces an earlier range.

    Only messages strictly before the current turn can be summarized, so the
    turn moves earlier by however many messages the substitution removed.
    """
    if current_turn_start < record.retained_from:
        raise ValueError("compaction must not reach into the current turn")
    return current_turn_start - record.retained_from + 1


@dataclass(frozen=True, slots=True)
class ContextRequest:
    messages: list[Message]
    tokens_before: TokenCount
    tokens_after: TokenCount
    omitted_turns: int
    omitted_messages: int
    memory_selected_ids: tuple[str, ...] = ()
    memory_included_ids: tuple[str, ...] = ()
    memory_dropped_ids: tuple[str, ...] = ()
    memory_tokens: int = 0
    #: Whether a compaction record replaced part of the history in this view.
    compacted: bool = False
    #: Messages removed from this view by the substitution, not by turn trimming.
    compacted_messages: int = 0
    #: Tokens this view would have carried without the substitution.
    compaction_tokens_saved: int = 0
    #: Tool results shortened by rule folding, inside the current turn.
    folded_tool_results: int = 0


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


def _foldable_results(history: list[Message], current_turn_start: int) -> list[int]:
    """Indices of settled tool results inside the current turn, oldest first.

    Only results at or after ``current_turn_start`` are candidates here, because
    every older turn has already been omitted. Oldest-first order is what keeps the
    newest output readable for as long as possible: folding stops as soon as the
    request fits, so the latest result is only shortened when nothing else worked.
    """
    return [
        index
        for index in range(current_turn_start, len(history))
        if history[index].role == "tool" and _is_settled(history, index)
    ]


def _is_settled(history: list[Message], index: int) -> bool:
    """Whether a tool result's own group is complete and cannot still be pending."""
    call_id = history[index].tool_call_id
    if call_id is None:
        return False
    return any(call_id == call.id for call in _preceding_calls(history, index))


def _preceding_calls(history: list[Message], index: int) -> list[ToolCall]:
    for position in range(index - 1, -1, -1):
        if history[position].tool_calls:
            return list(history[position].tool_calls)
    return []


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


@dataclass(frozen=True, slots=True)
class _Omission:
    """How much older history a view already leaves out."""

    turns: int
    messages: int


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
        project_memory: MemoryContext | None = None,
        compaction: CompactionRecord | None = None,
    ) -> ContextRequest:
        """Build the request view, substituting a settled range when compacted.

        Compaction is applied first and only to the request view. Turn trimming
        then runs over the substituted history, so a compacted conversation still
        drops whole old turns exactly as before. ``history`` is never mutated.
        """
        source = (
            history if compaction is None else apply_replacement(history, compaction)
        )
        turn_start = (
            current_turn_start
            if compaction is None
            else _shift(current_turn_start, compaction)
        )
        baseline = self._with_memory(
            self._build_baseline(
                system_messages=system_messages,
                history=source,
                current_turn_start=turn_start,
                tools=tools,
            ),
            project_memory,
            system_messages,
            tools,
        )
        if compaction is None:
            return baseline
        # Measured against the conversation as recorded, so the saving is the
        # whole benefit of the substitution rather than the part turn trimming
        # happened to leave behind.
        uncompacted = self._count([*system_messages, *history], tools)
        return replace(
            baseline,
            compacted=True,
            compacted_messages=compaction.retained_from,
            compaction_tokens_saved=max(
                0, uncompacted.tokens - baseline.tokens_after.tokens
            ),
        )

    def _with_memory(
        self,
        baseline: ContextRequest,
        project_memory: MemoryContext | None,
        system_messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> ContextRequest:
        if project_memory is None or not project_memory.items:
            return baseline
        # Preserve the measured baseline history suffix. Optional evidence can
        # never cause another round of history trimming or split a tool group.
        prefix = len(system_messages) + bool(baseline.omitted_turns)
        included: tuple[MemoryContextItem, ...] = ()
        dropped: list[str] = []
        messages = baseline.messages
        count = baseline.tokens_after
        for item in project_memory.items:
            proposed = (*included, item)
            view = [
                *baseline.messages[:prefix],
                Message(role="system", content=MEMORY_DATA_POLICY),
                Message(role="user", content=render_memory(proposed)),
                *baseline.messages[prefix:],
            ]
            measured = self._count(view, tools)
            cost = max(0, measured.tokens - baseline.tokens_after.tokens)
            if cost <= project_memory.max_tokens and self._fits(measured):
                included, messages, count = proposed, view, measured
            else:
                dropped.append(item.id)
        return replace(
            baseline,
            messages=messages,
            tokens_after=count,
            memory_selected_ids=tuple(item.id for item in project_memory.items),
            memory_included_ids=tuple(item.id for item in included),
            memory_dropped_ids=tuple(dropped),
            memory_tokens=max(0, count.tokens - baseline.tokens_after.tokens),
        )

    def _build_folded(
        self,
        *,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
        omission: _Omission,
        tokens_before: TokenCount,
    ) -> ContextRequest | None:
        """Fold current-turn tool output until the request fits, or give up.

        This is the only reduction left once every old turn has been omitted. It
        folds the oldest settled tool results first and stops as soon as a view
        fits, so the most recent output stays readable. A tool result is never
        dropped, only shortened, which keeps every call paired.

        Returns ``None`` when folding cannot make the request fit; the caller then
        reports the budget error it would have reported before.
        """
        folded = list(history)
        notice = (
            []
            if omission.turns == 0
            else [_omission_notice(omission.turns, omission.messages)]
        )
        folded_count = 0
        for index in _foldable_results(history, current_turn_start):
            original = folded[index]
            folded[index] = original.model_copy(
                update={"content": folded_placeholder(len(original.content or ""))}
            )
            folded_count += 1
            view = [*system_messages, *notice, *folded]
            count = self._count(view, tools)
            if not self._fits(count):
                continue
            return ContextRequest(
                messages=view,
                tokens_before=tokens_before,
                tokens_after=count,
                omitted_turns=omission.turns,
                omitted_messages=omission.messages,
                folded_tool_results=folded_count,
            )
        return None

    def _build_baseline(
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
            # A first-turn overflow has no old turn to omit, so folding this
            # turn's settled tool output is the only reduction available.
            folded = self._build_folded(
                system_messages=system_messages,
                history=history,
                current_turn_start=current_turn_start,
                tools=tools,
                omission=_Omission(turns=0, messages=0),
                tokens_before=tokens_before,
            )
            if folded is not None:
                return folded
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
            # Nothing is left to omit, so the only remaining reduction is to fold
            # settled tool output inside the current turn. Whole groups stay
            # paired and the user's own message is untouched.
            folded = self._build_folded(
                system_messages=system_messages,
                history=history,
                current_turn_start=current_turn_start,
                tools=tools,
                omission=_Omission(
                    turns=max_omitted, messages=old_turn_ends[max_omitted - 1]
                ),
                tokens_before=tokens_before,
            )
            if folded is not None:
                return folded
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
