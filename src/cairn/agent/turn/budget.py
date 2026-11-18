"""Measure a turn and decide safe-boundary budget stops, without owning lifecycle."""

from __future__ import annotations

import hashlib
import json
from math import floor, isfinite
from time import monotonic
from typing import Any, NoReturn

from cairn.agent.budget import BudgetReason, BudgetUsage, RunBudget, RunBudgetExceeded
from cairn.agent.context.window import ContextRequest
from cairn.agent.models.messages import Message, ToolCall
from cairn.agent.turn.events import Event, EventHandler
from cairn.agent.turn.usage import UsageLedger


class TurnBudgetPolicy:
    """Apply a ``RunBudget`` to one turn, reading usage from its ledger.

    Budget semantics for ``max_tokens`` are explicit:

    * no new request starts once the total is spent (``check``);
    * while the remaining ceiling covers the request's measured input, the
      provider is asked for at most the difference (``output_limit``);
    * a request that cannot fit is still admitted as the turn's last payment,
      because refusing it could not recover what the conversation already cost,
      and the resulting overrun is recorded when the turn finishes
      (``finish``). A post-hoc check can record an overspend; it cannot undo it.
    """

    def __init__(
        self,
        budget: RunBudget,
        emit: EventHandler,
        usage: UsageLedger | None = None,
    ) -> None:
        self.budget = budget
        self.emit = emit
        self.usage = usage if usage is not None else UsageLedger()
        self.started = monotonic()
        self.steps = 0
        self.tool_attempts = 0
        self.stagnant = 0
        self.previous: str | None = None
        self._terminal_request = False
        self.stopped: tuple[BudgetReason, int | float, int | float] | None = None

    def snapshot(self) -> BudgetUsage:
        totals = self.usage.totals()
        measured = totals.total_tokens
        if measured is None:
            # Nothing attempted is a real zero. Anything else attempted but
            # unmeasured stays unknown rather than being reported as zero.
            zero = totals.attempts == 0
            input_tokens: int | None = 0 if zero else None
            output_tokens: int | None = 0 if zero else None
            total_tokens: int | None = 0 if zero else None
        else:
            input_tokens = totals.input_tokens
            output_tokens = totals.output_tokens
            total_tokens = measured
        cost = self._cost(input_tokens, output_tokens)
        return BudgetUsage(
            self.steps,
            totals.attempts,
            self.tool_attempts,
            round(monotonic() - self.started, 6),
            input_tokens,
            output_tokens,
            total_tokens,
            cost,
            self.stagnant,
            self.stopped[0].value if self.stopped else None,
        )

    def _cost(
        self, input_tokens: int | None, output_tokens: int | None
    ) -> float | None:
        rates = (
            self.budget.input_cost_per_million,
            self.budget.output_cost_per_million,
        )
        if input_tokens is None or output_tokens is None:
            return None
        if rates[0] is None or rates[1] is None:
            return None
        value = (input_tokens * rates[0] + output_tokens * rates[1]) / 1000000
        return value if isfinite(value) else None

    def remaining_seconds(self) -> float | None:
        if self.budget.max_seconds is None:
            return None
        return max(0, self.budget.max_seconds - (monotonic() - self.started))

    @property
    def has_spend_limit(self) -> bool:
        return self.budget.max_tokens is not None or self.budget.max_cost is not None

    def output_limit(self, request: ContextRequest) -> int | None:
        """Admit a candidate, allowing one final request when its input won't fit."""
        self.check()
        usage = self.snapshot()
        if self.budget.max_tokens is None or usage.total_tokens is None:
            return None
        remaining = self.budget.max_tokens - usage.total_tokens
        output = remaining - request.tokens_after.tokens
        if output > 0:
            return output
        self._terminal_request = True
        return None

    def summary_output_limit(
        self, input_tokens: int, next_input_tokens: int, allowance: int
    ) -> int:
        """Reserve the summary's input and leave room for the primary reply."""
        self.check()
        usage = self.snapshot()
        budget = self.budget
        required = input_tokens + next_input_tokens
        if budget.max_tokens is not None and usage.total_tokens is not None:
            allowance = min(
                allowance, budget.max_tokens - usage.total_tokens - required - 1
            )
        if budget.max_cost is not None:
            assert usage.estimated_cost is not None
            input_cost = self._cost(required, 1)
            if input_cost is None:
                return 0
            remaining = budget.max_cost - usage.estimated_cost - input_cost
            if remaining <= 0:
                return 0
            rate = budget.output_cost_per_million
            assert rate is not None
            unit_cost = rate / 1_000_000
            if unit_cost and allowance * unit_cost > remaining:
                allowance = floor(remaining / unit_cost)
        return allowance

    def observe_batch(
        self, calls: tuple[ToolCall, ...], results: list[Message], authority: Any
    ) -> None:
        evidence = [
            [(call.name, call.arguments) for call in calls],
            [message.content for message in results],
            sorted(str(value) for value in getattr(authority, "grants", ())),
            sorted(str(value) for value in getattr(authority, "granted_roots", ())),
        ]
        digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.stagnant = self.stagnant + 1 if digest == self.previous else 0
        self.previous = digest

    def stop(
        self, reason: BudgetReason, limit: int | float, used: int | float
    ) -> NoReturn:
        self._record(reason, limit, used)
        assert self.stopped is not None
        reason, limit, used = self.stopped
        raise RunBudgetExceeded(reason=reason, limit=limit, used=used)

    def _record(
        self, reason: BudgetReason, limit: int | float, used: int | float
    ) -> None:
        """Keep the stop fact, announcing it only the first time."""
        if self.stopped is not None:
            return
        self.stopped = (reason, limit, used)
        self.emit(
            Event(
                type="agent_budget_exhausted",
                data={
                    "reason": reason.value,
                    "limit": limit,
                    "used": used,
                },
            )
        )

    def finish(self, *, truncated: bool = False) -> None:
        """Record an output limit or overrun revealed by the final response.

        The answer already exists and was already paid for. Discarding it would
        not recover the cost, so the turn completes with the overrun recorded in
        ``last_budget_usage`` rather than ending as a budget failure.
        """
        if self.stopped is not None:
            return
        usage = self.snapshot()
        if self.has_spend_limit and usage.total_tokens is None and usage.requests:
            self._note(BudgetReason.USAGE_UNKNOWN, 0, usage.requests)
        elif (
            self.budget.max_tokens is not None
            and usage.total_tokens is not None
            and (self._terminal_request or usage.total_tokens >= self.budget.max_tokens)
        ):
            self._note(
                BudgetReason.MAX_TOKENS, self.budget.max_tokens, usage.total_tokens
            )
        elif (
            self.budget.max_cost is not None
            and usage.estimated_cost is not None
            and usage.estimated_cost >= self.budget.max_cost
        ):
            self._note(
                BudgetReason.MAX_COST, self.budget.max_cost, usage.estimated_cost
            )
        elif truncated:
            self._note(BudgetReason.OUTPUT_LIMIT, 0, 0)

    def _note(
        self, reason: BudgetReason, limit: int | float, used: int | float
    ) -> None:
        """Record an overrun without announcing a stop that did not happen.

        The turn is completing normally; only the usage snapshot and the Run
        metrics need the fact.
        """
        self.stopped = (reason, limit, used)

    def check(self) -> None:
        usage = self.snapshot()
        budget = self.budget
        if (
            budget.max_seconds is not None
            and usage.elapsed_seconds >= budget.max_seconds
        ):
            self.stop(
                BudgetReason.MAX_SECONDS, budget.max_seconds, usage.elapsed_seconds
            )
        if usage.requests and self.has_spend_limit and usage.total_tokens is None:
            self.stop(BudgetReason.USAGE_UNKNOWN, 0, usage.requests)
        if (
            budget.max_tokens is not None
            and usage.total_tokens is not None
            and (self._terminal_request or usage.total_tokens >= budget.max_tokens)
        ):
            self.stop(BudgetReason.MAX_TOKENS, budget.max_tokens, usage.total_tokens)
        if budget.max_cost is not None:
            if usage.requests and usage.estimated_cost is None:
                self.stop(BudgetReason.USAGE_UNKNOWN, 0, usage.requests)
            if (
                usage.estimated_cost is not None
                and usage.estimated_cost >= budget.max_cost
            ):
                self.stop(BudgetReason.MAX_COST, budget.max_cost, usage.estimated_cost)
        if (
            budget.max_stagnant_steps is not None
            and self.stagnant >= budget.max_stagnant_steps
        ):
            self.stop(
                BudgetReason.NO_PROGRESS, budget.max_stagnant_steps, self.stagnant
            )
