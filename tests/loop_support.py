from typing import Any

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.observability.models import Span
from cairn.tools.base import Tool
from cairn.tools.registry import ToolRegistry


class SequenceLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[dict[str, Any]] | None]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append((messages, tools))
        return self.responses.pop(0)


class FailingLLM:
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        raise RuntimeError("llm failed")


class RecordingTool:
    name = "record"
    description = "Record arguments and return a deterministic result."

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(stdout="recorded", exit_code=0)


class FailingTool(RecordingTool):
    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("tool failed")


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def emit(self, span: Span) -> None:
        self.spans.append(span)


def make_agent(
    llm: SequenceLLM,
    tool: Tool | None = None,
    events: list[Event] | None = None,
) -> Agent:
    registry = ToolRegistry()
    if tool is not None:
        registry.register_tool(tool)

    def handle_event(event: Event) -> None:
        if events is not None:
            events.append(event)

    return Agent(
        llm=llm,
        tools=registry,
        event_handler=None if events is None else handle_event,
    )


def tool_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="record",
                arguments={"value": 42},
            )
        ]
    )
