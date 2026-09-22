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

        self.sink.emit(span)
