"""Measure a turn and decide safe-boundary budget stops, without owning lifecycle."""

from __future__ import annotations

import hashlib
import json
from math import isfinite
from time import monotonic
from typing import Any, NoReturn

from cairn.agent.budget import BudgetReason, BudgetUsage, RunBudget, RunBudgetExceeded
from cairn.agent.models.messages import LLMResponse, Message, ToolCall
from cairn.agent.turn.events import Event, EventHandler


class TurnBudgetPolicy:
    def __init__(self, budget: RunBudget, emit: EventHandler) -> None:
        self.budget = budget
        self.emit = emit
        self.started = monotonic()
        self.steps = 0
        self.requests = 0
        self.responses = 0
        self.tool_attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.known = True
        self.stagnant = 0
        self.previous: str | None = None
        self.stopped: tuple[BudgetReason, int | float, int | float] | None = None

    def snapshot(self) -> BudgetUsage:
        known = self.known and self.requests == self.responses
        total = self.input_tokens + self.output_tokens if known else None
        rates = (
            self.budget.input_cost_per_million,
            self.budget.output_cost_per_million,
        )
        cost = (
            (self.input_tokens * rates[0] + self.output_tokens * rates[1]) / 1000000
            if known and rates[0] is not None and rates[1] is not None
            else None
        )
        if cost is not None and not isfinite(cost):
            cost = None
        return BudgetUsage(
            self.steps,
            self.requests,
            self.tool_attempts,
            round(monotonic() - self.started, 6),
            self.input_tokens if known else None,
            self.output_tokens if known else None,
            total,
            cost,
            self.stagnant,
            self.stopped[0].value if self.stopped else None,
        )

    def remaining_seconds(self) -> float | None:
        if self.budget.max_seconds is None:
            return None
        return max(0, self.budget.max_seconds - (monotonic() - self.started))

    def observe_response(self, response: LLMResponse) -> None:
        self.responses += 1
        usage = response.usage
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            self.known = False
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

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
        if self.stopped is None:
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
        reason, limit, used = self.stopped
        raise RunBudgetExceeded(reason=reason, limit=limit, used=used)

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
        if (
            self.requests
            and (budget.max_tokens is not None or budget.max_cost is not None)
            and usage.total_tokens is None
        ):
            self.stop(BudgetReason.USAGE_UNKNOWN, 0, self.requests)
        if (
            budget.max_tokens is not None
            and usage.total_tokens is not None
            and usage.total_tokens >= budget.max_tokens
        ):
            self.stop(BudgetReason.MAX_TOKENS, budget.max_tokens, usage.total_tokens)
        if budget.max_cost is not None:
            if self.requests and usage.estimated_cost is None:
                self.stop(BudgetReason.USAGE_UNKNOWN, 0, self.requests)
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
