from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


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


class TraceSummary(BaseModel):
    trace_id: str
    name: str
    start_time: datetime
    end_time: datetime
    status: Literal[SpanStatus.OK, SpanStatus.ERROR]

    @model_validator(mode="after")
    def validate_timezones(self) -> "TraceSummary":
        if (self.start_time.utcoffset() is None) != (self.end_time.utcoffset() is None):
            raise ValueError("Summary timestamps must use consistent timezones")
        return self

    @classmethod
    def from_root(cls, span: Span) -> "TraceSummary":
        if span.context.parent_span_id is not None or span.end_time is None:
            raise ValueError("Expected a completed root span")
        return cls.model_validate(
            {
                "trace_id": span.context.trace_id,
                "name": span.name,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "status": span.status,
            }
        )


class TraceListResult(BaseModel):
    traces: list[TraceSummary] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


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
