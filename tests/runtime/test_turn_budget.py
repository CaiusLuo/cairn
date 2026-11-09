import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.agent.budget import BudgetReason, RunBudget, RunBudgetExceeded
from cairn.agent.models.messages import LLMResponse, LLMUsage, ToolCall, ToolResult
from cairn.agent.tools.base import ToolExecutionContext
from cairn.agent.turn.events import Event
from cairn.agent.turn.loop import run_turn
from tests.support.runtime import RecordingTool, SequenceLLM, make_agent


def response(ident: str = "call", usage: bool = True) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=ident, name="record", arguments={})],
        usage=LLMUsage(input_tokens=20, output_tokens=10) if usage else None,
    )


@pytest.mark.parametrize(
    ("budget", "reason"),
    [
        (RunBudget(50, max_tokens=30), BudgetReason.MAX_TOKENS),
        (
            RunBudget(
                50,
                max_cost=0.00003,
                input_cost_per_million=1,
                output_cost_per_million=1,
            ),
            BudgetReason.MAX_COST,
        ),
        (RunBudget(50, max_stagnant_steps=2), BudgetReason.NO_PROGRESS),
    ],
)
def test_policy_stops_before_another_request(
    budget: RunBudget, reason: BudgetReason
) -> None:
    async def scenario() -> None:
        llm = SequenceLLM([response(str(i)) for i in range(10)])
        tool = RecordingTool()
        events: list[Event] = []
        agent = make_agent(llm, tool, events)
        with pytest.raises(RunBudgetExceeded) as error:
            await run_turn(agent, "record", budget=budget)
        assert error.value.reason is reason
        assert agent.last_budget_usage is not None
        assert agent.last_budget_usage.stop_reason == reason.value
        assert agent.last_budget_usage.total_tokens == 30 * len(llm.calls)
        assert len(tool.calls) == (3 if reason is BudgetReason.NO_PROGRESS else 0)
        assert any(e.type == "agent_budget_exhausted" for e in events)

    asyncio.run(scenario())


def test_unknown_usage_stays_unknown_and_blocks_cost_controlled_continuation() -> None:
    async def scenario() -> None:
        llm = SequenceLLM([response(usage=False)])
        agent = make_agent(llm, RecordingTool())
        with pytest.raises(RunBudgetExceeded) as error:
            await run_turn(agent, "record", budget=RunBudget(50, max_tokens=100))
        assert error.value.reason is BudgetReason.USAGE_UNKNOWN
        assert agent.last_budget_usage is not None
        assert agent.last_budget_usage.total_tokens is None
        assert agent.last_budget_usage.estimated_cost is None

    asyncio.run(scenario())


def test_wall_budget_cancels_tool_and_waits_for_its_cleanup() -> None:
    async def scenario() -> None:
        cleaned = asyncio.Event()

        class SlowTool(RecordingTool):
            async def execute(
                self,
                arguments: dict[str, Any],
                *,
                context: ToolExecutionContext | None = None,
            ) -> ToolResult:
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.02)
                    cleaned.set()
                raise AssertionError("unreachable")

        agent = make_agent(SequenceLLM([response()]), SlowTool())
        with pytest.raises(RunBudgetExceeded) as error:
            await asyncio.wait_for(
                run_turn(agent, "record", budget=RunBudget(50, max_seconds=0.1)), 2
            )
        assert error.value.reason is BudgetReason.MAX_SECONDS
        assert cleaned.is_set()
        assert '"ToolCancelled"' in (agent.state.messages[-1].content or "")
        assert agent.last_budget_usage is not None
        assert agent.last_budget_usage.tool_attempts == 1

    asyncio.run(scenario())


def test_durable_fence_capacity_pauses_before_unknown_effects(tmp_path: Path) -> None:
    from cairn.agent.session.models import MAX_INVOCATIONS
    from tests.support.durable_sessions import create_session

    async def scenario() -> None:
        llm = SequenceLLM([response(str(i)) for i in range(MAX_INVOCATIONS + 1)])
        tool = RecordingTool()
        agent = make_agent(llm, tool)
        _api, session = create_session(tmp_path, agent)
        try:
            with pytest.raises(RunBudgetExceeded) as error:
                await run_turn(agent, "record", budget=RunBudget(1000), session=session)
            assert error.value.reason is BudgetReason.CHECKPOINT_CAPACITY
            assert len(tool.calls) == MAX_INVOCATIONS
            report = session.inspect()
            assert (
                report.status == "clean"
                and report.checkpoint.outcome == "budget_exhausted"
            )
            assert report.unresolved is None
            assert agent.last_budget_usage is not None
            assert agent.last_budget_usage.stop_reason == "checkpoint_capacity"
        finally:
            session.close()

    asyncio.run(scenario())
