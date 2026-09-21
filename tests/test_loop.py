import asyncio
import json
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import PermissionDecision
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


def _agent(
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


def _tool_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="record",
                arguments={"value": 42},
            )
        ]
    )


def test_run_turn_returns_direct_model_response() -> None:
    events: list[Event] = []
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = _agent(llm, events=events)

    result = asyncio.run(run_turn(agent, "hello"))

    assert result == "done"
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert [event.type for event in events] == ["agent_step", "agent_finish"]
    assert llm.calls[0][0][0].role == "system"


def test_run_turn_executes_tool_and_returns_follow_up() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response(), LLMResponse(content="finished")])
    tool = RecordingTool()
    agent = _agent(llm, tool, events)

    result = asyncio.run(run_turn(agent, "use the tool"))

    assert result == "finished"
    assert tool.calls == [{"value": 42}]
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert json.loads(agent.state.messages[2].content or "") == {
        "stdout": "recorded",
        "stderr": "",
        "exit_code": 0,
    }
    assert [event.type for event in events] == [
        "agent_step",
        "tool_call",
        "tool_result",
        "agent_step",
        "agent_finish",
    ]


def test_run_turn_records_permission_denial_without_executing_tool() -> None:
    llm = SequenceLLM([_tool_response(), LLMResponse(content="denied handled")])
    tool = RecordingTool()
    agent = _agent(llm, tool)

    def deny(tool_call: ToolCall) -> PermissionDecision:
        assert tool_call.name == "record"
        return PermissionDecision.DENY

    agent.permission_handler = deny

    result = asyncio.run(run_turn(agent, "do not run it"))

    assert result == "denied handled"
    assert tool.calls == []
    assert json.loads(agent.state.messages[2].content or "") == {
        "error": "Permission denied by user.",
        "type": "PermissionDenied",
    }


def test_run_turn_records_tool_errors_and_continues() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response(), LLMResponse(content="recovered")])
    agent = _agent(llm, FailingTool(), events)

    result = asyncio.run(run_turn(agent, "run it"))

    assert result == "recovered"
    assert json.loads(agent.state.messages[2].content or "") == {
        "err": "tool failed",
        "type": "RuntimeError",
    }
    assert any(event.type == "tool_error" for event in events)


def test_run_turn_emits_and_raises_at_step_limit() -> None:
    events: list[Event] = []
    llm = SequenceLLM([_tool_response()])
    agent = _agent(llm, RecordingTool(), events)

    with pytest.raises(RuntimeError, match="Agent exceeded maximum steps: 1"):
        asyncio.run(run_turn(agent, "keep going", max_steps=1))

    assert events[-1] == Event(type="agent_step_limit", data={"max_steps": 1})
