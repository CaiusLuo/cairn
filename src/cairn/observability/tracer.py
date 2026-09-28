from datetime import UTC, datetime
from typing import Any

from cairn.observability.models import (
    Span,
    SpanStatus,
    child_context,
    new_trace_context,
    start_span,
)
from cairn.observability.sinks import TraceSink


class Tracer:
    def __init__(self, sink: TraceSink) -> None:
        self.sink = sink
        self._persistence_errors: dict[str, str] = {}

    def start_root_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        return start_span(
            name=name,
            context=new_trace_context(),
            attributes=attributes,
        )

    def start_child_span(
        self,
        parent: Span,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        return start_span(
            name=name,
            context=child_context(parent.context),
            attributes=attributes,
        )

    def end_span(
        self,
        span: Span,
        status: SpanStatus = SpanStatus.OK,
        error: str | None = None,
    ) -> None:
        span.end_time = datetime.now(UTC)
        span.status = status
        span.error = error

        trace_id = span.context.trace_id

        # Once persistence failed, the trace is already incomplete.
        # Keep finalizing spans in memory but do not repeatedly hit the broken sink.
        if trace_id in self._persistence_errors:
            return

        try:
            self.sink.emit(span)
        except Exception as exc:
            self._persistence_errors[trace_id] = f"{type(exc).__name__}: {exc}"

    def pop_persistence_error(self, trace_id: str) -> str | None:
        return self._persistence_errors.pop(trace_id, None)
