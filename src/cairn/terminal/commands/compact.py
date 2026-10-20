"""/compact: ask for a context compaction on demand.

The automatic path reduces a request only once it approaches the budget. This
command exists for the other case: a user who knows the conversation is about to
grow and would rather pay for the summary now. It reuses the same use case, so it
cannot produce a view the automatic path would reject.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import partial

from cairn.core.compaction import CompactionStatus
from cairn.runtime.turn.compaction import CompactionOutcome, CompactionStep

COMPACT_USAGE = "/compact"
COMPACT_SUMMARY = "Reduce the conversation context on demand."
COMPACT_DETAIL = (
    "Reduce the conversation now, without waiting for the context budget.\n"
    "Older settled history becomes one summary and oversized tool output\n"
    "is shortened in place; the original messages stay in the session.\n"
    "The current instruction and the most recent exchanges are unchanged.\n"
    "A turn still between tool calls is left alone, because its tool groups\n"
    "are not whole yet.\n"
    "A failed summary leaves the conversation as it was; nothing is lost.\n"
    "Running it again with no new conversation reuses the accepted\n"
    "summary and costs no model call; new content compacts again."
)


class CompactionReportKind(StrEnum):
    """The distinct things a manual compaction can report."""

    NOTHING_TO_DO = "nothing_to_do"
    SUMMARIZED = "summarized"
    FOLDED = "folded"
    #: The summary failed or did not help; the conversation is unchanged.
    UNAVAILABLE = "unavailable"


class CompactionReport:
    """What one manual compaction did, in words a user can act on."""

    def __init__(self, outcome: CompactionOutcome) -> None:
        self.outcome = outcome

    @property
    def kind(self) -> CompactionReportKind:
        """The report's shape, so a caller can branch without reading prose."""
        outcome = self.outcome
        if not outcome.changed and outcome.step is CompactionStep.NOT_NEEDED:
            return CompactionReportKind.NOTHING_TO_DO
        if outcome.status is not None:
            return CompactionReportKind.UNAVAILABLE
        if outcome.step is CompactionStep.SUMMARIZED:
            return CompactionReportKind.SUMMARIZED
        return CompactionReportKind.FOLDED

    def describe(self) -> str:
        outcome = self.outcome
        if not outcome.changed and outcome.step is CompactionStep.NOT_NEEDED:
            return "Context already fits the budget; nothing to compact."
        if outcome.status is CompactionStatus.SUMMARIZER_FAILED:
            return (
                "Summary could not be produced; the conversation was left as it "
                f"was (about {outcome.tokens_before} tokens)."
            )
        if outcome.status is CompactionStatus.EXHAUSTED:
            return (
                "Nothing could be compacted without losing what is needed "
                f"(about {outcome.tokens_before} tokens)."
            )
        if outcome.step is CompactionStep.SUMMARIZED:
            return (
                f"Compacted: about {outcome.tokens_before} -> "
                f"{outcome.tokens_after} tokens, older history replaced by a "
                "summary."
            )
        return (
            f"Compacted: about {outcome.tokens_before} -> {outcome.tokens_after} "
            f"tokens, {outcome.folded_messages} oversized tool result(s) shortened."
        )


def handle_compact(
    compact_now: Callable[[], Awaitable[CompactionOutcome]] | None,
) -> Callable[[], Awaitable[None]] | None:
    """Return the pending interaction, or explain why it is unavailable.

    The CLI awaits what this returns, matching the other commands that need to
    perform work before printing. ``None`` means the session cannot compact.
    """
    if compact_now is None:
        print("Context compaction is not available in this session.")
        return None
    return partial(_report, compact_now)


async def _report(
    compact_now: Callable[[], Awaitable[CompactionOutcome]],
) -> None:
    print(CompactionReport(await compact_now()).describe())
