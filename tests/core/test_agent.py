from typing import Any

from cairn.core.agent import DEFAULT_SYSTEM_PROMPT, Agent
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


def test_default_system_prompt_contains_coding_workflow() -> None:
    prompt = DEFAULT_SYSTEM_PROMPT
    for expectation in (
        "Inspect before modifying",
        "repository status",
        "smallest coherent change",
        "Verify the result",
        "final diff",
        "Do not claim success without verification evidence",
        "Use tools to establish facts",
    ):
        assert expectation in prompt


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
