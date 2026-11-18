import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.agent.budget import BudgetReason, RunBudget, RunBudgetExceeded
from cairn.agent.context.window import ContextBudget, ContextBuilder
from cairn.agent.harness import Agent
from cairn.agent.models.executor import ModelExecutor
from cairn.agent.models.messages import (
    LLMResponse,
    LLMUsage,
    Message,
    ToolCall,
    ToolResult,
)
from cairn.agent.models.selection import ModelConfig, ModelManager, ProviderConfig
from cairn.agent.tools.base import ToolExecutionContext
from cairn.agent.tools.registry import ToolRegistry
from cairn.agent.turn.budget import TurnBudgetPolicy
from cairn.agent.turn.events import Event
from cairn.agent.turn.loop import run_turn
from cairn.agent.turn.usage import UsageLedger
from tests.support.compaction import CharacterCounter
from tests.support.runtime import RecordingTool, SequenceLLM, make_agent


class CappedLLM:
    """Answers one scripted response and records every per-request output cap."""

    def __init__(self, response: LLMResponse, caps: list[int] | None = None) -> None:
        self.response = response
        self.caps = caps if caps is not None else []

    def with_output_limit(self, max_output_tokens: int) -> "CappedLLM":
        self.caps.append(max_output_tokens)
        return CappedLLM(self.response, self.caps)

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        return self.response


class FlakyThenAnswer:
    """One provider failure, then an answer that reports its usage."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("provider unavailable")
        return LLMResponse(
            content="done", usage=LLMUsage(input_tokens=10, output_tokens=5)
        )


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


def test_final_answer_overrun_is_recorded_on_the_completed_turn() -> None:
    """A paid-for answer is kept, but the overspend is no longer invisible."""

    async def scenario() -> None:
        llm = SequenceLLM(
            [
                LLMResponse(
                    content="done",
                    usage=LLMUsage(input_tokens=15, output_tokens=20),
                )
            ]
        )
        agent = make_agent(llm, RecordingTool())

        answer = await run_turn(agent, "hello", budget=RunBudget(5, max_tokens=10))

        assert answer == "done"
        usage = agent.last_budget_usage
        assert usage is not None
        assert usage.total_tokens == 35
        # The turn completed, but the fact that its last request crossed the
        # ceiling is recorded rather than shown as an empty stop reason.
        assert usage.stop_reason == BudgetReason.MAX_TOKENS.value
        assert usage.input_tokens == 15 and usage.output_tokens == 20

    asyncio.run(scenario())


def test_request_output_is_capped_to_the_remaining_token_budget() -> None:
    """The provider is asked for at most what the ceiling still allows."""

    async def scenario() -> None:
        llm = CappedLLM(
            LLMResponse(content="done", usage=LLMUsage(input_tokens=3, output_tokens=2))
        )
        agent = Agent(
            llm=llm,
            tools=ToolRegistry(),
            system_prompt="sys",
            context_builder=ContextBuilder(
                budget=ContextBudget(max_tokens=1000, response_tokens=20),
                counter=CharacterCounter(),
            ),
        )

        # The measured request is 8 characters; 105 - 8 leaves 97.
        assert (
            await run_turn(agent, "hello", budget=RunBudget(5, max_tokens=105))
            == "done"
        )

        assert llm.caps == [97]
        usage = agent.last_budget_usage
        assert usage is not None
        assert usage.total_tokens == 5
        assert usage.stop_reason is None

    asyncio.run(scenario())


@pytest.mark.parametrize("capped", [False, True])
def test_fallback_failure_keeps_usage_unknown_instead_of_zero(capped: bool) -> None:
    """A failed candidate may have been billed; the total stays unknown."""

    async def scenario() -> None:
        from litellm.exceptions import RateLimitError

        class RateLimited(FlakyThenAnswer):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitError(
                        message="model request limit",
                        model="openai/one",
                        llm_provider="openai",
                    )
                return LLMResponse(
                    content="done", usage=LLMUsage(input_tokens=10, output_tokens=5)
                )

        flaky = RateLimited()
        manager = ModelManager(
            ProviderConfig(
                base_url="https://example.test/v1",
                api_key_env="TEST_KEY",
                model_config=(
                    ModelConfig("one", ("openai/one",)),
                    ModelConfig("two", ("openai/two",)),
                ),
            )
        )
        agent = Agent(
            llm=flaky,
            tools=ToolRegistry(),
            model_executor=ModelExecutor(
                manager, lambda _id: (flaky, agent.context_builder)
            ),
        )

        if capped:
            with pytest.raises(RunBudgetExceeded) as error:
                await run_turn(
                    agent, "hello", budget=RunBudget(5, max_tokens=1_000_000)
                )
            assert error.value.reason is BudgetReason.USAGE_UNKNOWN
        else:
            assert await run_turn(agent, "hello", budget=RunBudget(5)) == "done"

        usage = agent.last_budget_usage
        assert usage is not None
        assert flaky.calls == (1 if capped else 2)
        assert usage.total_tokens is None
        assert usage.stop_reason == (
            BudgetReason.USAGE_UNKNOWN.value if capped else None
        )

    asyncio.run(scenario())


def test_partial_usage_keeps_its_reported_component_in_the_budget_snapshot() -> None:
    usage = UsageLedger()
    usage.request()
    usage.response(LLMUsage(input_tokens=37))
    policy = TurnBudgetPolicy(
        RunBudget(5, input_cost_per_million=1.0, output_cost_per_million=2.0),
        emit=lambda event: None,
        usage=usage,
    )

    snapshot = policy.snapshot()

    assert snapshot.input_tokens == 37
    assert snapshot.output_tokens is None
    assert snapshot.total_tokens is None
    assert snapshot.estimated_cost is None
