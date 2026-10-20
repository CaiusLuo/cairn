"""The summarizer port: turn a settled slice of a conversation into one summary.

This is a core contract. It names what the compaction policy needs from a model
without deciding which model, provider or transport supplies it, so the rule that
builds a request stays testable with a stub and the adapter stays replaceable.
"""

from dataclasses import dataclass
from typing import Protocol

from cairn.core.models import LLMUsage, Message

#: A summary longer than this share of its token allowance is treated as a
#: failed attempt. Summaries are compressed facts, not prose: a model that spends
#: the whole allowance describing the conversation has not summarized it, and the
#: caller re-measures the real token cost anyway.
SUMMARY_LENGTH_SLACK = 4


#: Failure reasons are enumerated so a trace can record why without carrying any
#: conversation text or provider error detail.
FAILURE_EMPTY = "empty_summary"
FAILURE_TOO_LONG = "summary_too_long"
FAILURE_MODEL_ERROR = "model_error"
FAILURE_CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """One summarization attempt, successful or not.

    A failure is a value rather than an exception: a failed summary must leave the
    conversation usable, so the caller degrades instead of unwinding the turn.
    """

    text: str = ""
    usage: LLMUsage | None = None
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.failure is None and not self.text.strip():
            raise ValueError("a successful summary must have text")

    @property
    def ok(self) -> bool:
        return self.failure is None

    @classmethod
    def failed(cls, reason: str, usage: LLMUsage | None = None) -> "SummaryResult":
        return cls(text="", usage=usage, failure=reason)


def check_summary(text: str, *, max_tokens: int) -> SummaryResult:
    """Validate summarizer output against the allowance it was given.

    The length check is a cheap guard, not a tokenizer: the caller measures the
    real request after substitution and rejects a summary that did not help.
    """
    stripped = text.strip()
    if not stripped:
        return SummaryResult.failed(FAILURE_EMPTY)
    if len(stripped) > max_tokens * SUMMARY_LENGTH_SLACK:
        return SummaryResult.failed(FAILURE_TOO_LONG)
    return SummaryResult(text=stripped)


class Summarizer(Protocol):
    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        """Summarize a whole, settled conversation slice.

        ``messages`` always covers complete tool-call groups and never includes
        the current turn. Implementations must treat the content as quoted data,
        never as instructions, and must not call tools.
        """
        ...
