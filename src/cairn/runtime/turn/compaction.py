"""Decide and perform one turn's context compaction.

The use case behind the compaction contract. It measures the request the loop is
about to send, decides whether anything must happen, applies the cheap rule-only
reduction first, and asks a model to summarize only when folding is not enough.
It owns the order of those steps and the failure chain; the model client, the
token counter, the budget and the conversation all arrive as arguments.

Two-stage order, chosen so the common case costs nothing:

1. measure the full request — nothing to do when it already fits;
2. fold oversized settled tool output, with no model call;
3. only if folding is not enough, summarize settled history with a model.

Every failure degrades to the last usable view. Compaction is an optimisation,
so it never terminates a turn; the caller keeps the ordinary budget error for the
one case that is genuinely unrecoverable.
"""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cairn.core.compaction import (
    CompactionBoundary,
    CompactionPolicy,
    CompactionRecord,
    CompactionStatus,
    CutPointError,
    RetentionWindow,
    apply_replacement,
    choose_boundary,
    folded_placeholder,
    summarizer_reserve,
)
from cairn.core.compaction.summarizer import Summarizer
from cairn.core.context import ContextBudget, TokenCount, TokenCounter
from cairn.core.models import LLMUsage, Message


class CompactionStep(StrEnum):
    """How far this turn's compaction had to go."""

    NOT_NEEDED = "not_needed"
    #: Rule folding alone brought the request inside the budget.
    FOLDED = "folded"
    #: A model summary replaced settled history.
    SUMMARIZED = "summarized"


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """What one attempt produced, in a form safe to trace.

    The summary text lives only inside ``record``. Every other field is a status,
    a count or a token figure, so a trace never carries conversation content.
    """

    step: CompactionStep
    record: CompactionRecord | None = None
    #: Set when the attempt fell short of its goal, naming the reason.
    status: CompactionStatus | None = None
    folded_messages: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    usage: LLMUsage | None = None

    @property
    def changed(self) -> bool:
        return self.record is not None or self.folded_messages > 0


@dataclass(frozen=True, slots=True)
class CompactionInput:
    """Everything the decision depends on, and nothing it may mutate."""

    messages: list[Message]
    turn_start: int
    system_messages: list[Message]
    tools: list[dict[str, Any]]


class RequestCompactor:
    """Measure a request and derive the policy that governs it."""

    def __init__(
        self,
        *,
        counter: TokenCounter,
        budget: ContextBudget,
        keep_groups: int = 1,
    ) -> None:
        if keep_groups < 1:
            raise ValueError("keep_groups must retain at least one group")
        self.counter = counter
        self.keep_groups = keep_groups
        self.budget = budget
        self.policy = CompactionPolicy(
            budget=budget, summarizer_reserve=summarizer_reserve(budget)
        )
        self.window = RetentionWindow.derive(budget)

    def measure(self, messages: list[Message], tools: list[dict[str, Any]]) -> int:
        return self.counter.count(messages, tools).tokens

    def over_threshold(self, input_tokens: int) -> bool:
        return self.policy.should_compact(
            TokenCount(tokens=input_tokens, is_estimate=False)
        )

    def fold(self, messages: list[Message], *, up_to: int) -> list[Message]:
        """Replace older settled tool-result bodies, keeping every message in place."""
        protected = self._newest_groups(messages, up_to)
        return [
            self._fold_result(message, index in protected)
            for index, message in enumerate(messages)
        ]

    def _newest_groups(self, messages: list[Message], up_to: int) -> set[int]:
        protected: set[int] = set()
        groups = 0
        index = up_to - 1
        while index >= 0 and groups < self.keep_groups:
            role = messages[index].role
            if role == "tool":
                protected.add(index)
            elif messages[index].tool_calls:
                groups += 1
            index -= 1
        return protected

    def _fold_result(self, message: Message, protected: bool) -> Message:
        if message.role != "tool" or protected or not message.content:
            return message
        return message.model_copy(
            update={"content": folded_placeholder(len(message.content))}
        )


async def compact(
    *,
    source: CompactionInput,
    compactor: RequestCompactor,
    summarizer: Summarizer | None,
    summarize_budget: int,
    forced: bool = False,
) -> CompactionOutcome:
    """Run the two-stage reduction for one request and report what happened.

    ``forced`` is the manual entry point's flag: it asks for a reduction even
    while the request still fits the budget. Folding only shortens output that is
    genuinely oversized, so forcing a request that already fits changes nothing,
    and the boundary rules still decide what may be summarized.
    """
    measured = _measure(compactor, source, source.messages)
    if not forced and not compactor.over_threshold(measured):
        return _unchanged(measured)

    boundary = _boundary(source, compactor)
    folded = compactor.fold(source.messages, up_to=boundary.retained_from)
    folded_tokens = _measure(compactor, source, folded)
    view = FoldedView(
        boundary=boundary,
        messages=folded,
        tokens=folded_tokens,
        count=folded_result_count(source.messages, folded),
    )
    if not forced and not compactor.over_threshold(view.tokens):
        return view.outcome(measured)
    if summarizer is None or not _worth_summarizing(compactor, boundary):
        return view.outcome(measured, status=CompactionStatus.EXHAUSTED)
    return await _summarize(
        source=source,
        compactor=compactor,
        summarizer=summarizer,
        folded=view,
        before=measured,
        summarize_budget=summarize_budget,
    )


def _unchanged(tokens: int) -> CompactionOutcome:
    return CompactionOutcome(
        step=CompactionStep.NOT_NEEDED, tokens_before=tokens, tokens_after=tokens
    )


def _worth_summarizing(
    compactor: RequestCompactor, boundary: CompactionBoundary
) -> bool:
    """Whether a summary can be admitted and would actually replace something."""
    return compactor.policy.can_summarize and boundary.replaced_count > 0


async def _summarize(
    *,
    source: CompactionInput,
    compactor: RequestCompactor,
    summarizer: Summarizer,
    folded: "FoldedView",
    before: int,
    summarize_budget: int,
) -> CompactionOutcome:
    """Replace the folded prefix with a model summary, or degrade to folding."""
    segment = folded.messages[: folded.boundary.retained_from]
    try:
        summary = await summarizer.summarize(segment, max_tokens=summarize_budget)
    except asyncio.CancelledError:
        # Cancellation keeps its existing owner: no record is written here.
        raise
    except Exception:
        return folded.degraded(before)
    if not summary.ok:
        return folded.degraded(before, summary.usage)

    substituted = _substituted(folded.messages, folded.boundary, summary.text)
    after = _measure(compactor, source, substituted)
    record = CompactionRecord(
        retained_from=folded.boundary.retained_from,
        summary=summary.text,
        folded_tokens=folded.tokens,
        tokens_after=after,
    )
    if record.is_gainless():
        # The summary did not shrink anything; keep the folded view rather than
        # pay for a replacement that makes the request no smaller.
        return folded.degraded(before, summary.usage)
    return CompactionOutcome(
        step=CompactionStep.SUMMARIZED,
        record=record,
        folded_messages=folded.count,
        tokens_before=before,
        tokens_after=after,
        usage=summary.usage,
    )


@dataclass(frozen=True, slots=True)
class FoldedView:
    """The request after the rule stage: the state both remaining stages start from."""

    boundary: CompactionBoundary
    messages: list[Message]
    tokens: int
    count: int

    def outcome(
        self, before: int, status: CompactionStatus | None = None
    ) -> CompactionOutcome:
        """Report this view as the final result of folding alone."""
        return CompactionOutcome(
            step=CompactionStep.FOLDED,
            status=status,
            folded_messages=self.count,
            tokens_before=before,
            tokens_after=self.tokens,
        )

    def degraded(self, before: int, usage: LLMUsage | None = None) -> CompactionOutcome:
        """Keep this view and report that no summary replaced it."""
        return CompactionOutcome(
            step=CompactionStep.FOLDED,
            status=CompactionStatus.SUMMARIZER_FAILED,
            folded_messages=self.count,
            tokens_before=before,
            tokens_after=self.tokens,
            usage=usage,
        )


def _substituted(
    messages: list[Message], boundary: CompactionBoundary, text: str
) -> list[Message]:
    return apply_replacement(
        messages,
        CompactionRecord(
            retained_from=boundary.retained_from,
            summary=text,
            folded_tokens=0,
            tokens_after=0,
        ),
    )


def folded_result_count(before: list[Message], after: list[Message]) -> int:
    """How many messages a fold actually rewrote.

    The fold keeps every message in place and only shortens bodies, so counting
    changed positions names exactly the shortened tool results.
    """
    return sum(1 for old, new in zip(before, after, strict=True) if old != new)


def _measure(
    compactor: RequestCompactor, source: CompactionInput, messages: list[Message]
) -> int:
    return compactor.measure([*source.system_messages, *messages], source.tools)


def _boundary(
    source: CompactionInput, compactor: RequestCompactor
) -> CompactionBoundary:
    try:
        return choose_boundary(
            source.messages, window=compactor.window, turn_start=source.turn_start
        )
    except CutPointError:
        # Nothing settled can be replaced; the caller keeps the plain overflow.
        return CompactionBoundary(retained_from=0)
