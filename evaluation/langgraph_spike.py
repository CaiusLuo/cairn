"""Opt-in, stateless LangGraph experiment; never imported by production entries.

Run the offline comparison with::

    uv run --group langgraph-spike python -m evaluation.langgraph_spike
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from cairn.core.budget import BudgetReason, RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBuilder
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import PermissionHandler
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.loop import _settle_aborted_turn, run_turn
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.tool_execution import execute_tool_call
from cairn.runtime.turn.context import build_system_messages
from cairn.runtime.turn_trace import AttemptContext, TurnTrace


class GraphState(TypedDict):
    steps: int
    finished: bool


@dataclass(frozen=True)
class SpikeResult:
    answer: str
    model_steps: int
    nodes: tuple[str, ...]


class LangGraphSpike:
    """One invocation owns a fresh Agent, with no Session/checkpoint/resume.

    Inject Cairn's already configured clients/builders/executor, not credentials
    or a second provider implementation. The Agent is deliberately not exposed:
    neither a production Session nor another turn can acquire this run's state.
    """

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
        self._agent = Agent(
            llm,
            tools,
            permission_handler=permission_handler,
            context_builder=context_builder,
            model_executor=model_executor,
            event_handler=event_handler,
        )
        self._started = False

    @property
    def messages(self) -> tuple[Message, ...]:
        """Read-only snapshots include settled facts after failure/cancellation."""
        return tuple(m.model_copy(deep=True) for m in self._agent.state.messages)

    async def run(self, user_input: str, *, budget: RunBudget) -> SpikeResult:
        if self._started:
            raise RuntimeError("A spike instance can run only once; no resume support.")
        self._started = True
        agent = self._agent
        trace = TurnTrace(agent, budget)
        pending: deque[ToolCall] = deque()
        executed = False
        answer = ""
        nodes: list[str] = []
        error: str | None = None

        async def model_node(state: GraphState) -> GraphState:
            nonlocal pending, answer
            nodes.append("model")
            system = await build_system_messages(agent)
            schemas = agent.tools.schemas()
            trace.begin_step(
                AttemptContext(state["steps"] + 1, len(schemas), len(system), None)
            )
            try:
                _, response = await agent.model_executor.execute(
                    primary_runtime=(agent.llm, agent.context_builder),
                    system_messages=system,
                    history=agent.state.messages,
                    current_turn_start=0,
                    tools=schemas,
                    on_request=trace.on_request,
                    on_fallback=trace.on_failure,
                    on_stream=agent.emit,
                )
            except (asyncio.CancelledError, Exception) as exc:
                trace.on_failure(exc)
                raise
            trace.on_response(response)
            agent.state.add_assistant_message(
                response.content, tool_calls=response.tool_calls
            )
            pending = deque(response.tool_calls)
            answer = response.content or ""
            return {"steps": state["steps"] + 1, "finished": not pending}

        def mark_executed() -> None:
            nonlocal executed
            executed = True

        def consume_call() -> None:
            pending.popleft()

        async def tool_node(state: GraphState) -> GraphState:
            nodes.append("tools")
            while pending:
                await execute_tool_call(
                    agent,
                    pending[0],
                    trace.turn_span,
                    mark_tool_execution_started=mark_executed,
                    consume_tool_call=consume_call,
                    trusted_user_text=user_input,
                )
            return state

        def after_model(state: GraphState) -> Literal["tools", "end"]:
            return "end" if state["finished"] else "tools"

        def after_tools(state: GraphState) -> Literal["model", "end"]:
            return "end" if state["steps"] >= budget.max_steps else "model"

        graph = StateGraph(GraphState)
        graph.add_node("model", model_node)
        graph.add_node("tools", tool_node)
        graph.add_edge(START, "model")
        graph.add_conditional_edges(
            "model", after_model, {"tools": "tools", "end": END}
        )
        graph.add_conditional_edges(
            "tools", after_tools, {"model": "model", "end": END}
        )
        # No checkpointer, store, cache or retry policy. Graph state holds only
        # control flow; Cairn still owns the messages and execution authority.
        compiled = graph.compile()
        running: asyncio.Task[dict[str, Any]] | None = None
        try:
            trace.start()
            agent.state.add_user_message(user_input)
            # Disable environment-enabled LangSmith export, including for child
            # tasks, without changing the host's environment or configuration.
            with tracing_context(enabled=False):
                running = asyncio.create_task(
                    compiled.ainvoke(
                        {"steps": 0, "finished": False},
                        config={"recursion_limit": 2 * budget.max_steps + 2},
                    )
                )
                state = await asyncio.shield(running)
            if not state["finished"]:
                raise RunBudgetExceeded(
                    reason=BudgetReason.MAX_STEPS,
                    limit=budget.max_steps,
                    used=state["steps"],
                )
            return SpikeResult(answer, state["steps"], tuple(nodes))
        except (asyncio.CancelledError, Exception) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if running is not None:
                await settle_owned(running)
            # Temporary reuse of a private seam, explicitly recorded in the
            # experiment report; do not invent a second rollback implementation.
            _settle_aborted_turn(agent, pending, executed, 0, error)
            raise
        finally:
            trace.close(error)


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
    for name in ("cairn_loop", "langgraph"):
        model = OfflineModel()
        tool = FixtureReadTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        budget = RunBudget(max_steps=3)
        if name == "cairn_loop":
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
