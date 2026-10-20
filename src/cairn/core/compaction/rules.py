"""Pure rules for deciding when to compact and where a request may be cut.

Nothing here performs I/O, measures tokens or talks to a model. Callers supply
the measurements; these functions supply the policy, so every rule is testable
by enumerating message shapes instead of mocking an adapter.

The rules rest on two invariants:

* a request may only drop a range that ends on a group boundary, so no retained
  suffix ever holds a tool result whose call was dropped;
* the current turn's own user message is never dropped, so the instruction being
  worked on is always sent verbatim.
"""

from dataclasses import dataclass
from enum import StrEnum

from cairn.core.compaction.budget_values import ContextBudget, TokenCount
from cairn.core.models import Message

#: Smallest summarizer allowance the policy accepts. A summarizer that cannot
#: emit a usable summary makes compaction pointless, so the reserve must be able
#: to hold a real summary rather than an arbitrary positive number.
MINIMUM_SUMMARIZER_RESERVE_TOKENS = 1024

#: Default share of the input budget kept free for the summarization request.
DEFAULT_SUMMARIZER_RESERVE_RATIO = 0.1

#: Default share of the input budget whose most recent messages are kept verbatim.
DEFAULT_RETENTION_RATIO = 0.2


class CutPointError(ValueError):
    """A requested boundary would produce a request a provider must reject."""


class CompactionStatus(StrEnum):
    """Outcome of one compaction attempt, as recorded on the turn trace."""

    COMPLETED = "completed"
    #: The summarizer failed, returned nothing usable, or the view grew.
    SUMMARIZER_FAILED = "summarizer_failed"
    #: Nothing could free enough budget; the caller keeps the plain overflow.
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Derive the compaction threshold from a request budget.

    The threshold is a function of the budget, never a constant: a session bound
    to a smaller model must compact earlier, and model fallback re-admits each
    candidate with its own budget and therefore its own threshold.
    """

    budget: ContextBudget
    #: Tokens kept free so the summarization request itself can be admitted.
    summarizer_reserve: int

    def __post_init__(self) -> None:
        if type(self.summarizer_reserve) is not int:
            raise ValueError("summarizer_reserve must be an integer")
        if self.summarizer_reserve < MINIMUM_SUMMARIZER_RESERVE_TOKENS:
            raise ValueError(
                "summarizer_reserve must be at least "
                f"{MINIMUM_SUMMARIZER_RESERVE_TOKENS} tokens"
            )
        # A budget smaller than one summarization request is legal: the policy
        # then only ever folds, which ``can_summarize`` reports.

    @property
    def input_limit(self) -> int:
        """Largest input the request may carry and still be admitted."""
        return self.budget.input_tokens

    @property
    def can_summarize(self) -> bool:
        """Whether this budget leaves room for a summary request at all.

        A budget too small to admit one summarization and the conversation it
        must keep is a legitimate configuration, not an error: compaction then
        reduces by rules alone. Raising here would turn a working tiny-budget
        session into a failure at the first tool batch.
        """
        return (
            self.summarizer_reserve + self.budget.response_tokens
            < self.budget.max_tokens
        )

    @property
    def threshold(self) -> int:
        """Input size at which compaction must run.

        Compaction has to be admitted *and* leave a useful conversation behind,
        so it starts one summarizer reserve below the admissible input, not at it.
        A budget that cannot admit a summary keeps the ordinary input limit, so
        rule folding still runs once the request stops fitting.
        """
        if not self.can_summarize:
            return self.input_limit
        return self.input_limit - self.summarizer_reserve

    def should_compact(self, measured: TokenCount) -> bool:
        """Whether this request must be reduced before it is sent.

        Whether a legal boundary exists is the caller's question: it needs the
        conversation to answer, and only the caller has one.
        """
        return measured.tokens > self.threshold


def summarizer_reserve(
    budget: ContextBudget,
    *,
    ratio: float = DEFAULT_SUMMARIZER_RESERVE_RATIO,
) -> int:
    """Scale the summarizer allowance to the input budget.

    Clamped to the admissible input so a tiny test budget still yields a policy
    instead of an impossible one, and floored so the summary request is real.
    """
    return _scaled(budget, ratio, floor=MINIMUM_SUMMARIZER_RESERVE_TOKENS)


@dataclass(frozen=True, slots=True)
class RetentionWindow:
    """How much of a request stays verbatim, derived from the same budget.

    A fixed number of messages cannot serve both a small and a large model, and a
    bare ratio alone lets a tiny budget retain almost nothing. The window is a
    share of the input budget, clamped so it always keeps at least one complete
    tool-call group and never keeps so much that compaction frees nothing.
    """

    #: Tokens of the most recent conversation kept verbatim.
    tokens: int
    #: Whole tool-call groups retained regardless of size.
    minimum_groups: int

    def __post_init__(self) -> None:
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("retention tokens must be a nonnegative integer")
        if self.minimum_groups < 1:
            raise ValueError("at least one complete tool group must be retained")

    @classmethod
    def derive(
        cls,
        budget: ContextBudget,
        *,
        ratio: float = DEFAULT_RETENTION_RATIO,
        minimum_groups: int = 1,
    ) -> "RetentionWindow":
        """Derive the verbatim window, leaving the summary room to pay for itself."""
        floor = max(1, budget.input_tokens // 8)
        ceiling = max(floor, budget.input_tokens - MINIMUM_SUMMARIZER_RESERVE_TOKENS)
        return cls(
            tokens=min(ceiling, max(floor, _scaled(budget, ratio, floor=floor))),
            minimum_groups=minimum_groups,
        )


def _scaled(budget: ContextBudget, ratio: float, *, floor: int) -> int:
    if not 0 < ratio <= 1:
        raise ValueError("ratio must be in (0, 1]")
    scaled = int(budget.input_tokens * ratio)
    return max(floor, min(scaled, budget.input_tokens - 1))


def is_cut_point(messages: list[Message], index: int) -> bool:
    """Whether ``messages[:index]`` is a whole, provider-valid prefix.

    A suffix may only start where a new group starts. A tool result always
    continues the group above it, and an assistant message that issues tool calls
    is followed by its results, so neither may start a suffix. A plain assistant
    message ends a group and may start one.
    """
    if not 0 <= index < len(messages):
        return False
    message = messages[index]
    if message.role == "tool":
        return False
    return not (message.role == "assistant" and message.tool_calls)


def legal_cut_points(messages: list[Message], *, upper: int) -> list[int]:
    """Every cut index below ``upper`` that yields a valid retained suffix."""
    return [index for index in range(max(0, upper)) if is_cut_point(messages, index)]


@dataclass(frozen=True, slots=True)
class CompactionBoundary:
    """Where one compaction divides the conversation.

    The conversation splits at a single index. ``retained_from`` names it:

    * ``messages[:retained_from]`` is replaced by one summary message;
    * ``messages[retained_from:]`` is sent unchanged, keeping the current turn
      verbatim.

    Deriving both sides from one index keeps them consistent by construction; an
    earlier design stored two indices and let them disagree.
    """

    retained_from: int

    def __post_init__(self) -> None:
        if self.retained_from < 0:
            raise ValueError("retained_from must not be negative")

    @property
    def replaced_count(self) -> int:
        """How many messages the summary replaces."""
        return self.retained_from


def choose_boundary(
    messages: list[Message],
    *,
    window: RetentionWindow,
    turn_start: int,
) -> CompactionBoundary:
    """Choose the split point for compaction, never reaching the current turn.

    Selection rule, applied to the legal cut points strictly before ``turn_start``:

    1. never cut at index zero — replacing nothing is not a compaction;
    2. prefer points whose retained suffix holds at least
       ``window.minimum_groups`` complete tool groups;
    3. among the preferred points take the **earliest**, so compaction replaces as
       much old history as the group floor allows and repeated runs absorb old
       turns instead of creeping forward one at a time.

    The group floor is a preference, not a precondition. A conversation with no
    tool groups at all (plain question and answer) can still be shortened, and
    refusing it would mean compaction never applies to the commonest case.

    Raises ``CutPointError`` only when nothing before the current turn can be
    replaced.
    """
    if not 0 <= turn_start <= len(messages):
        raise ValueError("turn_start must index the current turn")
    legal = [cut for cut in legal_cut_points(messages, upper=turn_start) if cut > 0]
    if not legal:
        raise CutPointError("no legal cut point can compact before the current turn")
    preferred = [
        cut for cut in legal if count_groups(messages[cut:]) >= window.minimum_groups
    ]
    return CompactionBoundary(retained_from=preferred[0] if preferred else legal[0])


def count_groups(messages: list[Message]) -> int:
    """Count complete tool-call groups: a call plus every result that answers it."""
    groups = 0
    pending: set[str] | None = None
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            pending = {call.id for call in message.tool_calls}
            continue
        if message.role == "tool" and pending is not None:
            pending.discard(message.tool_call_id or "")
            if not pending:
                groups += 1
                pending = None
            continue
        pending = None
    return groups


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """One compaction already applied to a conversation.

    Immutable, append-only evidence: the conversation it describes is never
    rewritten, so a later compaction extends a list of these instead of
    replacing one.
    """

    #: Index of the first message the summary did not replace. Everything before
    #: it was replaced by one summary message, so this is also the count replaced.
    retained_from: int
    summary: str
    #: Request size once rule folding ran, which is what the summary replaced.
    folded_tokens: int
    #: Request size after substituting the summary.
    tokens_after: int

    def __post_init__(self) -> None:
        if self.retained_from < 1:
            raise ValueError("a compaction must replace at least one message")
        if self.folded_tokens < 0 or self.tokens_after < 0:
            raise ValueError("token counts must not be negative")
        if not self.summary.strip():
            raise ValueError("a compaction record requires a nonblank summary")

    def is_gainless(self) -> bool:
        """Whether this compaction failed to shrink the request."""
        return self.tokens_after >= self.folded_tokens
