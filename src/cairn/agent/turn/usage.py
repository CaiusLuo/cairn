"""One turn's model-call ledger, shared by its trace, budget and metrics.

Every model call a turn makes is recorded here: ordinary requests (including a
request that fell back to another candidate) and the summarization request a
compaction may issue. The trace, the budget policy and the durable/Run metrics
read the same totals from this object, so they cannot report different numbers.

Two rules make an incomplete record honest:

* a completed response that omitted usage leaves the total unknown, never zero;
* a request that produced no response leaves the total unknown too, because the
  provider may still have billed for it.

``requests`` and ``responses`` stay separate for exactly that reason: equality is
what proves every issued request was measured.
"""

from dataclasses import dataclass

from cairn.agent.models.messages import LLMUsage


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """What one turn's model calls consumed, or ``None`` where it is unknown."""

    requests: int
    responses: int
    summaries: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None

    @property
    def attempts(self) -> int:
        """Every call this turn issued, ordinary and summarization alike."""
        return self.requests + self.summaries

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class UsageLedger:
    """Accumulate one turn's calls and the usage each one reported."""

    def __init__(self) -> None:
        self._requests = 0
        self._responses = 0
        self._summaries = 0
        self._summary_responses = 0
        self._unreported = 0
        self._input = 0
        self._output = 0
        self._cached: int | None = 0
        self._reasoning: int | None = 0

    def request(self) -> None:
        """Record that one ordinary model request is about to be sent."""
        self._requests += 1

    def response(self, usage: LLMUsage | None) -> None:
        """Record a completed ordinary response and the usage it reported."""
        self._responses += 1
        self._consume(usage)

    def summary_request(self) -> None:
        self._summaries += 1

    def summary_response(self, usage: LLMUsage | None) -> None:
        self._summary_responses += 1
        self._consume(usage)

    def totals(self) -> UsageTotals:
        known = (
            self._requests == self._responses
            and self._summaries == self._summary_responses
            and self._unreported == 0
        )
        reported = self._responses + self._summaries > 0
        measured = known and reported
        return UsageTotals(
            requests=self._requests,
            responses=self._responses,
            summaries=self._summaries,
            input_tokens=self._input if measured else None,
            output_tokens=self._output if measured else None,
            cached_tokens=self._cached if measured else None,
            reasoning_tokens=self._reasoning if measured else None,
        )

    def _consume(self, usage: LLMUsage | None) -> None:
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            self._unreported += 1
            # Optional counters share the response's budget: once one response
            # could not be measured, a partial sum would imply a measurement
            # that was never made.
            self._cached = None
            self._reasoning = None
            return
        self._input += usage.input_tokens
        self._output += usage.output_tokens
        # Cache hits and reasoning tokens are reported only by some providers;
        # a turn where no response reported one keeps None rather than zero.
        self._cached = (
            None
            if self._cached is None or usage.cached_tokens is None
            else self._cached + usage.cached_tokens
        )
        self._reasoning = (
            None
            if self._reasoning is None or usage.reasoning_tokens is None
            else self._reasoning + usage.reasoning_tokens
        )
