from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.models import LLMResponse
from cairn.tools.registry import ToolRegistry


class FakeLLM:
    async def generate(
        self,
        messages,
        tools=None,
    ) -> LLMResponse:
        return LLMResponse(
            content="fake response"
        )


def test_agent_emit():
    received = []

    def handler(event):
        received.append(event)

    agent = Agent(
        llm=FakeLLM(),
        tools=ToolRegistry(),
        event_handler=handler,
    )

    agent.emit(
        Event(
            type="tool_call",
            data={
                "tool": "bash",
                "arguments": {
                    "command": "pwd"
                },
            },
        )
    )

    assert len(received) == 1
    assert received[0].type == "tool_call"