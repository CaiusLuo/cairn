from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class SpanStatus(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class TraceContext(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None


class Span(BaseModel):
    context: TraceContext
    name: str
    start_time: datetime
    end_time: datetime | None = None

    status: SpanStatus = SpanStatus.UNSET

    attributes: dict[str, Any] = Field(
        default_factory=dict,
    )

    error: str | None = None


def new_trace_context() -> TraceContext:
    return TraceContext(
        trace_id=uuid4().hex,
        span_id=uuid4().hex[:16],
    )


def child_context(
    parent_context: TraceContext,
) -> TraceContext:
    return TraceContext(
        trace_id=parent_context.trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=parent_context.span_id,
    )


def start_span(
    name: str, context: TraceContext, attributes: dict[str, Any] | None = None
) -> Span:
    return Span(
        context=context,
        name=name,
        start_time=datetime.now(UTC),
        attributes=attributes or {},
    )
