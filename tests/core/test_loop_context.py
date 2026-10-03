import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    TokenCount,
)
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.repo.context import RepoContextProvider, RepositoryContext
from cairn.tools.base import ToolExecutionContext
from cairn.tools.registry import ToolRegistry
from cairn.workspace.workspace import Workspace
from tests.support.runtime import (
    TEST_BUDGET,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    tool_response,
)

SYSTEM = Message(role="system", content="Follow instructions.")
RESPONSE_RESERVE = 20
TRIM_NOTICE_ALLOWANCE = 300


class SerializedCounter:
    """Deterministic token units including all message and schema fields."""

    def __init__(self, *, is_estimate: bool = False) -> None:
        self.is_estimate = is_estimate

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        payload = {
            "messages": [message.model_dump() for message in messages],
            "tools": tools,
        }
        return TokenCount(
            tokens=len(json.dumps(payload, ensure_ascii=False)),
            is_estimate=self.is_estimate,
        )


class SchemaTool(RecordingTool):
    def __init__(self, description: str) -> None:
        super().__init__()
        self.description = description

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


def make_context_agent(
    llm: SequenceLLM,
    budget: ContextBudget,
    counter: SerializedCounter,
    *,
    tool: RecordingTool | None = None,
    events: list[Event] | None = None,
    sink: RecordingSink | None = None,
    system_prompt: str = SYSTEM.content or "",
) -> Agent:
    tools = ToolRegistry()
    if tool is not None:
        tools.register_tool(tool)
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        context_builder=ContextBuilder(budget=budget, counter=counter),
        event_handler=None if events is None else lambda event: events.append(event),
        tracer=None if sink is None else Tracer(sink),
    )


@pytest.mark.parametrize("is_estimate", [False, True])
def test_request_budget_includes_schema_and_reserve_and_records_trim(
    is_estimate: bool,
) -> None:
    counter = SerializedCounter(is_estimate=is_estimate)
    tool = SchemaTool("Schema context " * 30)
    schemas = [tool.schema()]
    current = Message(role="user", content="current question")
    request = [SYSTEM, current]
    request_tokens = counter.count(request, schemas).tokens
    context_budget = ContextBudget(
        max_tokens=request_tokens + RESPONSE_RESERVE + TRIM_NOTICE_ALLOWANCE,
        response_tokens=RESPONSE_RESERVE,
    )
    sink = RecordingSink()
    events: list[Event] = []
    llm = SequenceLLM([LLMResponse(content="answer")])
    agent = make_context_agent(
        llm, context_budget, counter, tool=tool, events=events, sink=sink
    )
    agent.state.add_user_message("previous question " + "x" * 500)
    agent.state.add_assistant_message("previous answer " + "x" * 500)
    previous = agent.state.messages.copy()
    before_tokens = counter.count([SYSTEM, *previous, current], schemas).tokens

    assert (
        asyncio.run(run_turn(agent, current.content or "", budget=TEST_BUDGET))
        == "answer"
    )

    assert len(llm.calls) == 1
    sent, sent_schemas = llm.calls[0]
    assert sent_schemas == schemas
    assert sent[0] == SYSTEM
    assert_trim_notice(sent[1])
    assert sent[2:] == [current]
    assert agent.state.messages == [
        *previous,
        current,
        Message(role="assistant", content="answer"),
    ]
    after_tokens = counter.count(sent, schemas).tokens
    assert after_tokens + RESPONSE_RESERVE <= (context_budget.max_tokens)
    llm_span = next(span for span in sink.spans if span.name == "llm.generate")
    assert llm_span.status == SpanStatus.OK
    assert llm_span.attributes == {
        "step": 1,
        "message_count": 3,
        "message_count_before": 4,
        "tool_schema_count": 1,
        "context_tokens_before": before_tokens,
        "context_tokens_after": after_tokens,
        "context_count_is_estimate": is_estimate,
        "context_max_tokens": context_budget.max_tokens,
        "context_response_tokens": RESPONSE_RESERVE,
        "context_omitted_turns": 1,
        "context_omitted_messages": 2,
        "tool_call_count": 0,
        "has_content": True,
    }
    trimmed = [event for event in events if event.type == "context_trimmed"]
    assert len(trimmed) == 1
    assert trimmed[0].data["omitted_turns"] == 1
    assert trimmed[0].data["omitted_messages"] == 2


def test_one_hundred_turns_keep_requests_bounded_and_full_history_intact() -> None:
    counter = SerializedCounter()
    users = [
        Message(role="user", content=f"question-{index:02} " + "x" * 300)
        for index in range(100)
    ]
    answers = [
        Message(role="assistant", content=f"answer-{index:02} " + "y" * 300)
        for index in range(100)
    ]
    retained = [SYSTEM, users[0], answers[0], users[1], answers[1], users[2]]
    context_budget = ContextBudget(
        max_tokens=counter.count(retained, []).tokens
        + RESPONSE_RESERVE
        + TRIM_NOTICE_ALLOWANCE,
        response_tokens=RESPONSE_RESERVE,
    )
    llm = SequenceLLM([LLMResponse(content=answer.content) for answer in answers])
    agent = make_context_agent(llm, context_budget, counter)

    async def scenario() -> None:
        expected_history: list[Message] = []
        for index, user in enumerate(users):
            assert await run_turn(agent, user.content or "", budget=TEST_BUDGET) == (
                answers[index].content
            )
            request, schemas = llm.calls[index]
            assert request[0] == SYSTEM
            if index > 2:
                assert_trim_notice(request[1])
                conversation = request[2:]
            else:
                conversation = request[1:]
            assert conversation == [*expected_history[-4:], user]
            assert schemas == []
            assert counter.count(request, schemas).tokens + RESPONSE_RESERVE <= (
                context_budget.max_tokens
            )
            assert len(request) <= 7
            expected_history.extend([user, answers[index]])
            assert agent.state.messages == expected_history

    asyncio.run(scenario())
    assert len(llm.calls) == 100
    assert len(agent.state.messages) == 200


def multi_tool_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"call-{index}", name="record", arguments={"value": index})
            for index in (1, 2)
        ]
    )


def assert_trim_notice(message: Message) -> None:
    assert message.role == "system"
    content = message.content or ""
    assert "omitted" in content.lower()
    assert "current turn" in content
    assert "unavailable" in content


@pytest.mark.parametrize("failed_old_turn", [False, True])
def test_old_multi_tool_turn_is_retained_or_omitted_as_a_complete_group(
    failed_old_turn: bool,
) -> None:
    counter = SerializedCounter()
    tool = RecordingTool()
    llm = SequenceLLM([multi_tool_response(), LLMResponse(content="finished")])
    events: list[Event] = []
    agent = make_context_agent(
        llm,
        ContextBudget(max_tokens=10_000, response_tokens=RESPONSE_RESERVE),
        counter,
        tool=tool,
        events=events,
    )
    if failed_old_turn:
        with pytest.raises(RunBudgetExceeded):
            asyncio.run(
                run_turn(agent, "use tools " + "x" * 100, budget=RunBudget(max_steps=1))
            )
        assert agent.state.messages[-1].role == "tool"
    else:
        assert (
            asyncio.run(run_turn(agent, "use tools " + "x" * 100, budget=TEST_BUDGET))
            == "finished"
        )
    old_turn = agent.state.messages.copy()
    assert [message.tool_call_id for message in old_turn if message.role == "tool"] == [
        "call-1",
        "call-2",
    ]
    current = Message(role="user", content="continue")
    schemas = [tool.schema()]
    full_request = [SYSTEM, *old_turn, current]
    agent.context_builder = ContextBuilder(
        budget=ContextBudget(
            max_tokens=counter.count(full_request, schemas).tokens + RESPONSE_RESERVE,
            response_tokens=RESPONSE_RESERVE,
        ),
        counter=counter,
    )
    kept_llm = SequenceLLM([LLMResponse(content="continued")])
    agent.llm = kept_llm
    assert asyncio.run(run_turn(agent, "continue", budget=TEST_BUDGET)) == "continued"
    assert kept_llm.calls[0] == (full_request, schemas)

    latest_turn = [current, Message(role="assistant", content="continued")]
    latest_user = Message(role="user", content="trim old tools")
    latest_request = [SYSTEM, *latest_turn, latest_user]
    agent.context_builder = ContextBuilder(
        budget=ContextBudget(
            max_tokens=counter.count(latest_request, schemas).tokens
            + RESPONSE_RESERVE
            + TRIM_NOTICE_ALLOWANCE,
            response_tokens=RESPONSE_RESERVE,
        ),
        counter=counter,
    )
    dropped_llm = SequenceLLM([LLMResponse(content="done")])
    agent.llm = dropped_llm
    assert asyncio.run(run_turn(agent, "trim old tools", budget=TEST_BUDGET)) == "done"
    request, sent_schemas = dropped_llm.calls[0]
    assert sent_schemas == schemas
    assert request[0] == SYSTEM
    assert_trim_notice(request[1])
    assert request[2:] == latest_request[1:]
    assert agent.state.messages == [
        *old_turn,
        *latest_turn,
        latest_user,
        Message(role="assistant", content="done"),
    ]
    trimmed = [event for event in events if event.type == "context_trimmed"]
    assert len(trimmed) == 1
    assert trimmed[0].data["omitted_turns"] == 1
    assert trimmed[0].data["omitted_messages"] == len(old_turn)
    assert tool.calls == [{"value": 1}, {"value": 2}]


@pytest.mark.parametrize("oversized", ["input", "system", "schema"])
def test_required_context_overflow_never_calls_llm_and_rolls_back(
    oversized: str,
) -> None:
    counter = SerializedCounter()
    user_input = "x" * 1_000 if oversized == "input" else "current"
    system_prompt = "x" * 1_000 if oversized == "system" else SYSTEM.content or ""
    tool = SchemaTool("x" * 1_000 if oversized == "schema" else "small schema")
    required = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_input),
    ]
    required_tokens = counter.count(required, [tool.schema()]).tokens
    llm = SequenceLLM([LLMResponse(content="should not be requested")])
    sink = RecordingSink()
    events: list[Event] = []
    agent = make_context_agent(
        llm,
        ContextBudget(
            max_tokens=required_tokens + RESPONSE_RESERVE - 1,
            response_tokens=RESPONSE_RESERVE,
        ),
        counter,
        tool=tool,
        system_prompt=system_prompt,
        sink=sink,
        events=events,
    )
    agent.state.add_user_message("previous question")
    agent.state.add_assistant_message("previous answer")
    previous = agent.state.messages.copy()

    with pytest.raises(ContextBudgetExceeded, match=r"[Cc]ontext"):
        asyncio.run(run_turn(agent, user_input, budget=TEST_BUDGET))

    assert llm.calls == []
    assert tool.calls == []
    assert agent.state.messages == previous
    assert not any(event.type == "agent_finish" for event in events)
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert turn_span.status == SpanStatus.ERROR
    assert "ContextBudgetExceeded" in (turn_span.error or "")
    assert all(span.end_time is not None for span in sink.spans)
    assert events[-1].type == "trace_finish"
    assert events[-1].data["status"] == "error"


class LargeOutputTool(RecordingTool):
    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(
            stdout=f"result-{arguments['value']}:" + "x" * 1_000, exit_code=0
        )


def test_current_tool_output_overflow_preserves_executed_facts_and_can_recover() -> (
    None
):
    counter = SerializedCounter()
    tool = LargeOutputTool()
    current = Message(role="user", content="run both tools")
    initial_request = [SYSTEM, current]
    llm = SequenceLLM([multi_tool_response()])
    sink = RecordingSink()
    events: list[Event] = []
    agent = make_context_agent(
        llm,
        ContextBudget(
            max_tokens=counter.count(initial_request, [tool.schema()]).tokens
            + RESPONSE_RESERVE
            + TRIM_NOTICE_ALLOWANCE,
            response_tokens=RESPONSE_RESERVE,
        ),
        counter,
        tool=tool,
        sink=sink,
        events=events,
    )
    agent.state.add_user_message("previous question " + "x" * 300)
    agent.state.add_assistant_message("previous answer " + "x" * 300)
    previous = agent.state.messages.copy()

    with pytest.raises(ContextBudgetExceeded, match=r"[Cc]ontext"):
        asyncio.run(run_turn(agent, current.content or "", budget=TEST_BUDGET))

    assert len(llm.calls) == 1
    first_request = llm.calls[0][0]
    assert first_request[0] == SYSTEM
    assert_trim_notice(first_request[1])
    assert first_request[2:] == [current]
    assert tool.calls == [{"value": 1}, {"value": 2}]
    assert agent.state.messages[:2] == previous
    assert [message.role for message in agent.state.messages[2:]] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    for index, fact in enumerate(agent.state.messages[-2:], start=1):
        assert fact.tool_call_id == f"call-{index}"
        assert (
            json.loads(fact.content or "")
            == ToolResult(
                stdout=f"result-{index}:" + "x" * 1_000, exit_code=0
            ).model_dump()
        )
    assert sink.spans[-1].status == SpanStatus.ERROR
    assert "ContextBudgetExceeded" in (sink.spans[-1].error or "")
    assert events[-1].data["status"] == "error"
    assert not any(event.type == "agent_finish" for event in events)

    preserved = agent.state.messages.copy()
    agent.context_builder = ContextBuilder(
        budget=ContextBudget(max_tokens=10_000, response_tokens=RESPONSE_RESERVE),
        counter=counter,
    )
    next_llm = SequenceLLM([LLMResponse(content="recovered")])
    agent.llm = next_llm
    assert asyncio.run(run_turn(agent, "continue", budget=TEST_BUDGET)) == "recovered"
    assert next_llm.calls[0][0] == [
        SYSTEM,
        *preserved,
        Message(role="user", content="continue"),
    ]
    assert tool.calls == [{"value": 1}, {"value": 2}]


class ChangingRepoProvider(RepoContextProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(Workspace(root))
        self.calls = 0
        self.contexts = [
            RepositoryContext(
                workspace_root=root,
                repository_root=root,
                branch="main",
                dirty=True,
                changed_files=(path,),
            )
            for path in ("short.py", "changed-" + "x" * 1_200 + ".py")
        ]

    async def inspect(self) -> RepositoryContext:
        context = self.contexts[self.calls]
        self.calls += 1
        return context


def test_refreshed_repository_context_is_rebudgeted_before_each_generation(
    tmp_path: Path,
) -> None:
    counter = SerializedCounter()
    provider = ChangingRepoProvider(tmp_path)
    tool = RecordingTool()
    current = Message(role="user", content="run tool")
    response = tool_response()
    tool_fact = Message(
        role="tool",
        tool_call_id="call-1",
        content=json.dumps(ToolResult(stdout="recorded", exit_code=0).model_dump()),
    )
    current_turn = [
        current,
        Message(role="assistant", tool_calls=response.tool_calls),
        tool_fact,
    ]
    latest_context = Message(role="system", content=provider.contexts[1].to_prompt())
    second_request = [SYSTEM, latest_context, *current_turn]
    llm = SequenceLLM([response, LLMResponse(content="done")])
    sink = RecordingSink()
    agent = make_context_agent(
        llm,
        ContextBudget(
            max_tokens=counter.count(second_request, [tool.schema()]).tokens
            + RESPONSE_RESERVE
            + TRIM_NOTICE_ALLOWANCE,
            response_tokens=RESPONSE_RESERVE,
        ),
        counter,
        tool=tool,
        sink=sink,
    )
    agent.repo_context_provider = provider
    agent.state.add_user_message("old question " + "x" * 300)
    agent.state.add_assistant_message("old answer " + "x" * 300)
    previous = agent.state.messages.copy()

    assert asyncio.run(run_turn(agent, "run tool", budget=TEST_BUDGET)) == "done"

    assert provider.calls == 2
    assert llm.calls[0][0] == [
        SYSTEM,
        Message(role="system", content=provider.contexts[0].to_prompt()),
        *previous,
        current,
    ]
    sent_second_request = llm.calls[1][0]
    assert sent_second_request[:2] == second_request[:2]
    assert_trim_notice(sent_second_request[2])
    assert sent_second_request[3:] == current_turn
    assert agent.state.messages == [
        *previous,
        *current_turn,
        Message(role="assistant", content="done"),
    ]
    llm_spans = [span for span in sink.spans if span.name == "llm.generate"]
    assert [span.attributes["context_omitted_turns"] for span in llm_spans] == [0, 1]
    assert (
        llm_spans[1].attributes["context_tokens_after"]
        == counter.count(sent_second_request, [tool.schema()]).tokens
    )
