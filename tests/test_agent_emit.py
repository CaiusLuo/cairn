from typing import Any

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message
from cairn.tools.registry import ToolRegistry


class FakeLLM:
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="fake response")


def test_agent_emit() -> None:
    received: list[Event] = []

    def handler(event: Event) -> None:
        received.append(event)

    agent = Agent(
        llm=FakeLLM(),
        tools=ToolRegistry(),
        event_handler=handler,
    )

    event = Event(
        type="tool_call",
        data={
            "tool": "bash",
            "arguments": {"command": "pwd"},
        },
    )
    agent.emit(event)

    assert received == [event]


def test_agent_emit_without_handler_is_a_no_op() -> None:
    agent = Agent(
        llm=FakeLLM(),
        tools=ToolRegistry(),
    )

    agent.emit(Event(type="agent_finish"))
