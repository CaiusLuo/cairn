from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

EventType = Literal[
    "agent_step",
    "tool_call",
    "tool_result",
    "tool_error",
    "agent_finish",
    "agent_step_limit",
    "trace_start",
]


class Event(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)


class EventHandler(Protocol):
    def __call__(self, event: Event) -> None: ...
