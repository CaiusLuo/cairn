"""A steer joins the turn that is already running.

The behaviour a reader notices: an instruction typed while work is in progress must
reach the model *within that turn*, keeping the work already done. Starting a fresh
turn instead would throw away the tool results the operator is trying to redirect.

Two failures are worth guarding against explicitly, because both are silent:

* a message that arrives while a tool batch runs must land before the next model
  call, not after the turn ends;
* a message that arrives as the model is finishing must not be discarded by the
  early return that ends a turn.
"""

import asyncio
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.turn.steering import PendingMessage, SteerQueue
from tests.support.runtime import SequenceLLM

BUDGET = RunBudget(max_steps=6)


class _SlowTool:
    """A tool that takes long enough for a steer to arrive while it runs."""

    name = "slow"
    description = "sleeps briefly"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    def schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    def validate(self, arguments: dict[str, object]) -> None:
        return None

    async def execute(self, arguments: object, *, context: object = None) -> ToolResult:
        await asyncio.sleep(self.delay)
        return ToolResult(stdout="done", exit_code=0)


def _agent(responses: list[LLMResponse], *, delay: float = 0.05) -> Agent:
    registry = ToolRegistry()
    registry.register_tool(_SlowTool(delay))
    return Agent(llm=SequenceLLM(responses), tools=registry)


def _tool_then_answer() -> list[LLMResponse]:
    return [
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="slow", arguments={})],
        ),
        LLMResponse(content="final answer"),
    ]


def _user_texts(agent: Agent) -> list[str]:
    return [
        message.content or ""
        for message in agent.state.messages
        if message.role == "user"
    ]


def test_a_steer_reaches_the_model_within_the_same_turn() -> None:
    queue = SteerQueue()
    agent = _agent(_tool_then_answer())

    async def scenario() -> tuple[str, list[str]]:
        asyncio.get_running_loop().call_later(
            0.01, lambda: queue.push(PendingMessage(text="use the other module"))
        )
        result = await run_turn(agent, "start", budget=BUDGET, steering=queue)
        return result, _user_texts(agent)

    result, texts = asyncio.run(scenario())

    assert result == "final answer"
    assert texts == ["start", "use the other module"]


def test_a_turn_without_steering_is_unchanged() -> None:
    agent = _agent(_tool_then_answer())
    result = asyncio.run(run_turn(agent, "start", budget=BUDGET))
    assert result == "final answer"
    assert _user_texts(agent) == ["start"]


def test_a_queued_message_does_not_join_the_running_turn() -> None:
    """Queue means after the turn; it must not appear as a mid-turn instruction."""
    queue = SteerQueue()
    agent = _agent(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="slow", arguments={})],
            ),
            LLMResponse(content="final answer"),
        ]
    )

    async def scenario() -> list[str]:
        asyncio.get_running_loop().call_later(
            0.01,
            lambda: queue.push(PendingMessage(text="later", delivery="queue")),
        )
        await run_turn(agent, "start", budget=BUDGET, steering=queue)
        return _user_texts(agent)

    texts = asyncio.run(scenario())

    assert texts == ["start"]
    assert [item.text for item in queue.queued()] == ["later"]


def test_a_steer_arriving_as_the_model_finishes_is_not_lost() -> None:
    """The early return that ends a turn must check the queue first.

    The message arrives after the model has answered with no tool calls — the exact
    moment the loop would otherwise return. Dropping it would leave the operator
    believing they redirected work that continued unchanged.
    """
    queue = SteerQueue()

    class _PushOnFirstCall:
        """A client that queues a steer while producing its first answer."""

        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
        ) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                queue.push(PendingMessage(text="late instruction"))
                return LLMResponse(content=None, tool_calls=[])
            return LLMResponse(content="second answer")

    agent = Agent(llm=_PushOnFirstCall(), tools=ToolRegistry())

    result = asyncio.run(run_turn(agent, "start", budget=BUDGET, steering=queue))

    assert result == "second answer"
    assert "late instruction" in _user_texts(agent)


def test_an_empty_queue_leaves_the_turn_alone() -> None:
    agent = _agent(_tool_then_answer())
    queue = SteerQueue()
    asyncio.run(run_turn(agent, "start", budget=BUDGET, steering=queue))
    assert _user_texts(agent) == ["start"]


def test_two_steers_arrive_in_the_order_they_were_sent() -> None:
    queue = SteerQueue()
    agent = _agent(_tool_then_answer())

    async def scenario() -> list[str]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, lambda: queue.push(PendingMessage(text="first steer")))
        loop.call_later(0.02, lambda: queue.push(PendingMessage(text="second steer")))
        await run_turn(agent, "start", budget=BUDGET, steering=queue)
        return _user_texts(agent)

    texts = asyncio.run(scenario())

    assert texts == ["start", "first steer", "second steer"]


@pytest.mark.parametrize("delivery", ["steer", "queue"])
def test_the_queue_is_empty_after_a_turn(delivery: str) -> None:
    """A delivered message must not be replayed into the next turn."""
    queue = SteerQueue()
    agent = _agent(_tool_then_answer())
    queue.push(PendingMessage(text="typed", delivery=delivery))  # type: ignore[arg-type]

    asyncio.run(run_turn(agent, "start", budget=BUDGET, steering=queue))

    assert not queue.has_pending_steers
