from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "turn_started",
    "turn_settled",
    "turn_committed",
    "model_stream_start",
    "model_first_token",
    "context_prepare",
    "tool_execution_cancelled",
    "model_text_delta",
    "model_reasoning_delta",
    "model_stream_end",
    "model_request",
    "model_response",
    "model_error",
    "agent_step",
    "permission_check",
    "tool_call",
    "tool_result",
    "tool_error",
    "tool_denied",
    "agent_finish",
    "agent_budget_exhausted",
    "context_trimmed",
    "context_compacted",
    "trace_start",
    "trace_finish",
]


class Event(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)


class SessionEvent(Event):
    """Ordered live attribution; only turn_committed acknowledges a checkpoint."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    turn_id: UUID
    sequence: int = Field(ge=1)


class EventHandler(Protocol):
    def __call__(self, event: Event) -> None: ...
