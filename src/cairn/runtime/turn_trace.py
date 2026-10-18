"""What one Agent turn reports about itself.

Three modules divide a turn: ``run_turn`` owns the turn lifecycle and the
conversation rollback, ``tool_execution`` owns the per-tool sequence inside an
accepted response, and this module owns the third concern those two do not — the
root and per-attempt spans, the accumulated token usage, the context-trim events
and the final ``trace_finish`` event.

Keeping that in one object means the turn has a single owner for usage
accounting and trace finalization, and ``run_turn`` stays a readable sequence of
model calls and tool calls. A later Session/checkpoint boundary needs to know
what a completed turn reported; that answer lives here.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, ContextRequest
from cairn.core.events import Event
from cairn.core.memory.context import MemoryContext
from cairn.core.model_manager import ModelConfig
from cairn.core.models import LLMResponse
from cairn.infra.observability.models import Span, SpanStatus
from cairn.runtime.agent import Agent


@dataclass(frozen=True, slots=True)
class AttemptContext:
    """The loop-owned facts one model attempt needs to describe itself."""

    step: int
    tool_schema_count: int
    system_message_count: int
    memory: MemoryContext | None


@dataclass(frozen=True, slots=True)
class TurnTraceSummary:
    trace_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    persisted: bool | None = None
    error_code: Literal["sink_failed"] | None = None


class TurnTrace:
    """Own the spans, usage and trace events of exactly one turn."""

    def __init__(self, agent: Agent, budget: RunBudget) -> None:
        self._agent = agent
        self._budget = budget
        self._turn_span: Span | None = None
        self._llm_span: Span | None = None
        self._attempt: AttemptContext | None = None
        self._attempt_started = False
        self._response_count = 0
        self._reported_omitted_turns = 0
        self._input_tokens: int | None = 0
        self._output_tokens: int | None = 0
        self._cached_tokens: int | None = 0
        self._reasoning_tokens: int | None = 0
        self._summary: TurnTraceSummary | None = None

    @property
    def summary(self) -> TurnTraceSummary | None:
        """Available only after close and its final event return successfully."""
        return self._summary

    @property
    def turn_span(self) -> Span | None:
        """The turn's root span, for callers that parent their own spans on it."""
        return self._turn_span

    def _provider_attributes(self) -> dict[str, str]:
        """Trace-only provider identity, empty for provider-neutral agents."""
        if self._agent.provider_name is None:
            return {}
        return {"provider": self._agent.provider_name}

    def start(self) -> None:
        """Open the turn's root span and announce the trace, if tracing is on."""
        if self._agent.tracer is None:
            return
        self._turn_span = self._agent.tracer.start_root_span(
            "agent.turn",
            attributes={
                "max_steps": self._budget.max_steps,
                "context_max_tokens": self._agent.context_builder.budget.max_tokens,
                "context_response_tokens": (
                    self._agent.context_builder.budget.response_tokens
                ),
                **self._provider_attributes(),
            },
        )
        self._agent.emit(
            Event(
                type="trace_start",
                data={"trace_id": self._turn_span.context.trace_id},
            )
        )

    def begin_step(self, attempt: AttemptContext) -> None:
        """Discard the previous step's attempt state before the next call."""
        self._attempt = attempt
        self._llm_span = None
        self._attempt_started = False

    def on_request(
        self,
        request: ContextRequest,
        context_budget: ContextBudget,
        model: ModelConfig | None,
        model_id: str | None,
        attempt_number: int,
    ) -> None:
        """Record admission metadata and open the attempt span for one call."""
        self._attempt_started = True
        if (
            request.omitted_turns
            and request.omitted_turns != self._reported_omitted_turns
        ):
            self._agent.emit(
                Event(
                    type="context_trimmed",
                    data={
                        "omitted_turns": request.omitted_turns,
                        "omitted_messages": request.omitted_messages,
                    },
                )
            )
        self._reported_omitted_turns = request.omitted_turns

        if self._agent.tracer is None or self._turn_span is None:
            return
        context = self._attempt
        assert context is not None
        attributes: dict[str, Any] = {
            "step": context.step,
            "tool_schema_count": context.tool_schema_count,
            "message_count": len(request.messages),
            "message_count_before": context.system_message_count
            + len(self._agent.state.messages),
            "context_tokens_before": request.tokens_before.tokens,
            "context_tokens_after": request.tokens_after.tokens,
            "context_count_is_estimate": request.tokens_after.is_estimate,
            "context_max_tokens": context_budget.max_tokens,
            "context_response_tokens": context_budget.response_tokens,
            "context_omitted_turns": request.omitted_turns,
            "context_omitted_messages": request.omitted_messages,
            **self._provider_attributes(),
        }
        if model is not None:
            attributes.update(
                model=model_id, model_name=model.name, attempt=attempt_number
            )
        memory_context = context.memory
        if memory_context is not None:
            attributes.update(
                memory_selected_ids=list(request.memory_selected_ids),
                memory_included_ids=list(request.memory_included_ids),
                memory_dropped_ids=list(request.memory_dropped_ids),
                memory_tokens=request.memory_tokens,
                memory_count_is_estimate=request.tokens_after.is_estimate,
                memory_checked_count=memory_context.checked_count,
                memory_freshness_counts=dict(memory_context.freshness_counts),
                memory_dropped_for_size=memory_context.dropped_for_size,
                memory_read_error=memory_context.read_error,
            )
        self._llm_span = self._agent.tracer.start_child_span(
            self._turn_span, "llm.generate", attributes=attributes
        )

    def on_failure(self, exc: BaseException) -> None:
        """Close the attempt span after a call produced no response."""
        # No attempt means admission failed before any provider call, so usage
        # already accounted for by earlier steps must survive.
        if not self._attempt_started:
            return
        self._attempt_started = False
        self._input_tokens = None
        self._output_tokens = None
        self._cached_tokens = None
        self._reasoning_tokens = None
        if self._llm_span is not None and self._agent.tracer is not None:
            if isinstance(exc, asyncio.CancelledError):
                self._llm_span.attributes["cancelled"] = True
                error = "LLM generation was cancelled."
            else:
                error = f"{type(exc).__name__}: {exc}"
            self._agent.tracer.end_span(
                self._llm_span, status=SpanStatus.ERROR, error=error
            )
        # A fallback closes this attempt before the next one starts.
        self._llm_span = None

    def on_response(self, response: LLMResponse) -> None:
        """Accumulate usage and close the attempt span for a completed call."""
        self._response_count += 1
        if self._input_tokens is not None:
            if response.usage is None or response.usage.input_tokens is None:
                self._input_tokens = None
            else:
                self._input_tokens += response.usage.input_tokens
        if self._output_tokens is not None:
            if response.usage is None or response.usage.output_tokens is None:
                self._output_tokens = None
            else:
                self._output_tokens += response.usage.output_tokens
        # Cache hits and reasoning tokens are reported only by some providers;
        # a turn where no response reported one keeps None rather than zero, so
        # the display can omit the figure instead of implying a real measurement.
        for field in ("cached_tokens", "reasoning_tokens"):
            current = getattr(self, f"_{field}")
            if current is None:
                continue
            value = None if response.usage is None else getattr(response.usage, field)
            if value is None:
                setattr(self, f"_{field}", None)
            else:
                setattr(self, f"_{field}", current + value)

        if self._llm_span is None or self._agent.tracer is None:
            return
        self._llm_span.attributes["tool_call_count"] = len(response.tool_calls)
        self._llm_span.attributes["has_content"] = response.content is not None
        if response.usage is not None:
            if response.usage.input_tokens is not None:
                self._llm_span.attributes["input_tokens"] = response.usage.input_tokens
            if response.usage.output_tokens is not None:
                self._llm_span.attributes["output_tokens"] = (
                    response.usage.output_tokens
                )
        self._agent.tracer.end_span(self._llm_span, status=SpanStatus.OK)

    def close(self, trace_error: str | None) -> None:
        """Finalize the turn span, its usage and the ``trace_finish`` event.

        Raises whatever the trace sink raises: the caller owns the conversation
        rollback that a failed finalization requires.
        """
        if self._turn_span is None or self._agent.tracer is None:
            self._summary = TurnTraceSummary()
            return
        status = SpanStatus.OK if trace_error is None else SpanStatus.ERROR
        input_tokens = self._input_tokens if self._response_count else None
        output_tokens = self._output_tokens if self._response_count else None
        if input_tokens is not None:
            self._turn_span.attributes["input_tokens"] = input_tokens
        if output_tokens is not None:
            self._turn_span.attributes["output_tokens"] = output_tokens

        self._agent.tracer.end_span(self._turn_span, status=status, error=trace_error)

        persistence_error = self._agent.tracer.pop_persistence_error(
            self._turn_span.context.trace_id
        )
        trace_finish_data: dict[str, Any] = {
            "trace_id": self._turn_span.context.trace_id,
            "status": status.value,
            "persisted": persistence_error is None,
            "persistence_error": persistence_error,
        }
        usage: dict[str, int] = {}
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        cached_tokens = self._cached_tokens if self._response_count else None
        if cached_tokens is not None:
            usage["cached_tokens"] = cached_tokens
        reasoning_tokens = self._reasoning_tokens if self._response_count else None
        if reasoning_tokens is not None:
            usage["reasoning_tokens"] = reasoning_tokens
        if usage:
            trace_finish_data["usage"] = usage

        self._agent.emit(Event(type="trace_finish", data=trace_finish_data))
        self._summary = TurnTraceSummary(
            trace_id=self._turn_span.context.trace_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            persisted=persistence_error is None,
            error_code=None if persistence_error is None else "sink_failed",
        )
