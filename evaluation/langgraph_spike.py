"""Offline compatibility probe for the shared production run_turn owner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBuilder
from cairn.core.events import Event, EventHandler
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import PermissionHandler
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor


@dataclass(frozen=True)
class SpikeResult:
    answer: str
    model_steps: int
    nodes: tuple[str, ...]


class LangGraphSpike:
    """A single-use test facade; execution/lifecycle belong only to run_turn."""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        *,
        permission_handler: PermissionHandler | None = None,
        context_builder: ContextBuilder | None = None,
        model_executor: ModelExecutor | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self._nodes: list[str] = []

        def observe(event: Event) -> None:
            if event.type == "agent_step":
                self._nodes.append("model")
            elif event.type == "tool_call" and self._nodes[-1:] != ["tools"]:
                self._nodes.append("tools")
            if event_handler is not None:
                event_handler(event)

        self._agent = Agent(
            llm,
            tools,
            permission_handler=permission_handler,
            context_builder=context_builder,
            model_executor=model_executor,
            event_handler=observe,
        )
        self._started = False

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(
            message.model_copy(deep=True) for message in self._agent.state.messages
        )

    async def run(self, user_input: str, *, budget: RunBudget) -> SpikeResult:
        if self._started:
            raise RuntimeError("A probe instance can run only once")
        self._started = True
        answer = await run_turn(self._agent, user_input, budget=budget)
        return SpikeResult(answer, self._nodes.count("model"), tuple(self._nodes))


class FixtureReadTool:
    """Read a constant in memory; no filesystem, process or network effects."""

    name = "fixture_read"
    description = "Read the offline answer fixture."

    def __init__(self) -> None:
        self.calls = 0

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if arguments:
            raise InvalidArguments("fixture_read takes no arguments")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.calls += 1
        return ToolResult(stdout="offline fixture", exit_code=0)


class OfflineModel:
    """Deterministic LLMClient that consumes the actual tool reply."""

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.requests.append([m.model_copy(deep=True) for m in messages])
        if messages[-1].role == "tool":
            result = json.loads(messages[-1].content or "{}")
            return LLMResponse(content=f"Answer: {result['stdout']}")
        return LLMResponse(
            tool_calls=[ToolCall(id="read-1", name="fixture_read", arguments={})]
        )


async def compare_offline() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for name in ("direct_agent", "facade"):
        model = OfflineModel()
        tool = FixtureReadTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        budget = RunBudget(max_steps=3)
        if name == "direct_agent":
            answer = await run_turn(
                Agent(model, registry), "Read the fixture", budget=budget
            )
            nodes = None
        else:
            result = await LangGraphSpike(model, registry).run(
                "Read the fixture", budget=budget
            )
            answer, nodes = result.answer, result.nodes
        results[name] = {
            "answer": answer,
            "model_requests": len(model.requests),
            "tool_executions": tool.calls,
            "graph_nodes": nodes,
        }
    return results


if __name__ == "__main__":
    print(json.dumps(asyncio.run(compare_offline()), indent=2, ensure_ascii=False))
