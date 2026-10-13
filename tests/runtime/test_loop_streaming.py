import asyncio
from typing import Any

import pytest
from litellm.exceptions import RateLimitError

from cairn.core.events import Event
from cairn.core.llm import LLMStreamChunk, StreamProtocolError, ToolCallDelta
from cairn.core.models import Message, ToolCall, ToolResult
from cairn.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionResult,
)
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import StreamObserverError
from tests.support.runtime import (
    TEST_BUDGET,
    NetworkRequestTool,
    RecordingSink,
    RecordingTool,
)
from tests.support.streaming import ScriptedStreamLLM, StreamBarrier


def make_agent(
    llm: ScriptedStreamLLM, events: list[Event], tool: RecordingTool | None = None
) -> Agent:
    registry = ToolRegistry()
    if tool is not None:
        registry.register_tool(tool)

    def observe(event: Event) -> None:
        events.append(event)

    return Agent(llm=llm, tools=registry, event_handler=observe)


def test_tools_wait_for_full_response_and_closed_stream() -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        first = LLMStreamChunk(
            tool_calls=(ToolCallDelta(0, "a", "record", '{"value":1}'),)
        )
        llm = ScriptedStreamLLM(
            [
                first,
                blocked,
                LLMStreamChunk(
                    tool_calls=(ToolCallDelta(1, "b", "record", '{"value":2}'),)
                ),
                LLMStreamChunk(finish_reason="tool_calls"),
            ],
            [LLMStreamChunk(text="done"), LLMStreamChunk(finish_reason="stop")],
        )
        tool = RecordingTool()
        events: list[Event] = []
        agent = make_agent(llm, events, tool)
        task = asyncio.create_task(run_turn(agent, "hello", budget=TEST_BUDGET))
        await blocked.entered.wait()
        assert tool.calls == []
        assert agent.state.messages == [Message(role="user", content="hello")]
        blocked.release.set()
        assert await task == "done"
        assert tool.calls == [{"value": 1}, {"value": 2}]
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
        ]
        assert [
            message.tool_call_id
            for message in agent.state.messages
            if message.role == "tool"
        ] == ["a", "b"]
        assert llm.closed == 2
        transient = [event for event in events if event.type.startswith("model_")]
        assert all(
            {"step", "attempt", "model_id"} <= event.data.keys() for event in transient
        )
        assert not any("arguments" in event.data for event in transient)

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_accepted_group_keeps_preflight_and_capability_authority(allowed: bool) -> None:
    order: list[str] = []

    class CheckedTool(NetworkRequestTool):
        def validate(self, arguments: dict[str, Any]) -> None:
            order.append("preflight")

        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            assert context is not None and context.network_access is True
            order.append("execute")
            return await super().execute(arguments, context=context)

    tool = CheckedTool()
    llm = ScriptedStreamLLM(
        [
            LLMStreamChunk(
                tool_calls=(
                    ToolCallDelta(
                        0,
                        "call",
                        "bash",
                        '{"command":"curl example.com","network_access":true,"justification":"fetch"}',
                    ),
                )
            ),
            LLMStreamChunk(finish_reason="tool_calls"),
        ],
        [LLMStreamChunk(text="denied"), LLMStreamChunk(finish_reason="stop")],
    )
    events: list[Event] = []
    agent = make_agent(llm, events, tool)

    def deny(tool_call: ToolCall) -> PermissionResult:
        order.append("permission")
        assert llm.closed == 1
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=allowed,
            granted_capabilities=frozenset({PermissionCapability.NETWORK})
            if allowed
            else frozenset(),
        )

    agent.permission_handler = deny
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "denied"
    assert order == (
        ["preflight", "permission", "execute"]
        if allowed
        else ["preflight", "permission"]
    )
    assert len(tool.calls) == int(allowed)
    assert any(event.type == "tool_denied" for event in events) is (not allowed)


@pytest.mark.parametrize(
    "script",
    [
        [LLMStreamChunk(text="partial")],
        [LLMStreamChunk(text="partial", finish_reason="length")],
        [
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "record", "{"),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ],
        [
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "record", "{}"),)),
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "b"),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ],
        [
            LLMStreamChunk(
                tool_calls=(
                    ToolCallDelta(0, "a", "record", "{}"),
                    ToolCallDelta(1, "a", "record", "{}"),
                )
            ),
            LLMStreamChunk(finish_reason="tool_calls"),
        ],
        [LLMStreamChunk(tool_calls=(ToolCallDelta(1, "a", "record", "{}"),))],
        [LLMStreamChunk(finish_reason="stop"), LLMStreamChunk(text="late")],
        [LLMStreamChunk(text="partial"), RuntimeError("provider failed")],
        [LLMStreamChunk(text="a" * 1_048_577)],
        [
            LLMStreamChunk(
                tool_calls=(ToolCallDelta(0, "a", "record", " " * 1_048_577),)
            )
        ],
        [LLMStreamChunk(tool_calls=tuple(ToolCallDelta(index) for index in range(33)))],
        [LLMStreamChunk()] * 16_385,
    ],
)
def test_invalid_stream_commits_no_partial_messages_or_tool_effects(
    script: list[LLMStreamChunk | Exception],
) -> None:
    llm = ScriptedStreamLLM(script)
    events: list[Event] = []
    tool = RecordingTool()
    agent = make_agent(llm, events, tool)
    prior = [
        Message(role="user", content="prior question"),
        Message(role="assistant", content="prior turn"),
    ]
    agent.state.messages.extend(prior)
    with pytest.raises((StreamProtocolError, RuntimeError)):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert agent.state.messages == prior
    assert tool.calls == []
    assert llm.closed == 1
    assert events[-1].type == "model_stream_end"
    assert events[-1].data["status"] == "incomplete"


@pytest.mark.parametrize(
    "prefix",
    [
        [],
        [LLMStreamChunk(text="partial")],
        [LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "record", '{"value":'),))],
        [LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "record", "{}"),))],
        [
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "record", "{}"),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ],
    ],
)
def test_partial_cancellation_rolls_back_and_repeated_cancel_finishes_cleanup(
    prefix: list[LLMStreamChunk],
) -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        closing = StreamBarrier(asyncio.Event(), asyncio.Event())
        llm = ScriptedStreamLLM([*prefix, blocked])
        llm.cleanup_barrier = closing
        events: list[Event] = []
        tool = RecordingTool()
        agent = make_agent(llm, events, tool)
        task = asyncio.create_task(run_turn(agent, "hello", budget=TEST_BUDGET))
        await blocked.entered.wait()
        task.cancel()
        await closing.entered.wait()
        task.cancel()
        task.cancel()
        closing.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tool.calls == []
        assert agent.state.messages == []
        assert llm.closed == 1
        assert events[-1].data["status"] == "incomplete"
        assert not [
            owned
            for owned in asyncio.all_tasks()
            if owned is not asyncio.current_task() and not owned.done()
        ]

    asyncio.run(scenario())


def test_cancel_after_earlier_accepted_tool_step_preserves_settled_facts() -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        llm = ScriptedStreamLLM(
            [
                LLMStreamChunk(
                    tool_calls=(ToolCallDelta(0, "a", "record", '{"value":1}'),)
                ),
                LLMStreamChunk(finish_reason="tool_calls"),
            ],
            [LLMStreamChunk(text="unaccepted"), blocked],
        )
        tool = RecordingTool()
        events: list[Event] = []
        agent = make_agent(llm, events, tool)
        task = asyncio.create_task(run_turn(agent, "hello", budget=TEST_BUDGET))
        await blocked.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tool.calls == [{"value": 1}]
        assert [message.role for message in agent.state.messages] == [
            "user",
            "assistant",
            "tool",
        ]
        assert agent.state.messages[-1].tool_call_id == "a"
        assert all(message.content != "unaccepted" for message in agent.state.messages)
        assert llm.closed == 2

    asyncio.run(scenario())


def test_observer_failure_is_not_provider_retry_and_does_not_leak_callback_data() -> (
    None
):
    llm = ScriptedStreamLLM(
        [LLMStreamChunk(text="partial"), LLMStreamChunk(finish_reason="stop")]
    )
    events: list[Event] = []
    agent = make_agent(llm, events)

    def failing(event: Event) -> None:
        if event.type == "model_text_delta":
            raise RateLimitError(
                message="TOPSECRET", model="test", llm_provider="openai"
            )
        events.append(event)

    agent.event_handler = failing
    with pytest.raises(StreamObserverError) as error:
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert "TOPSECRET" not in str(error.value)
    assert agent.state.messages == []
    assert len(llm.calls) == llm.closed == 1


def test_provisional_tool_arguments_never_enter_trace_or_events() -> None:
    secret = "unaccepted-secret-argument"
    llm = ScriptedStreamLLM(
        [
            LLMStreamChunk(
                tool_calls=(ToolCallDelta(0, "a", "record", '{"value":"' + secret),)
            ),
            RuntimeError("failed"),
        ]
    )
    events: list[Event] = []
    agent = make_agent(llm, events)
    sink = RecordingSink()
    agent.tracer = Tracer(sink)
    with pytest.raises(RuntimeError):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert secret not in repr(events)
    assert secret not in repr(sink.spans)
    assert agent.state.messages == []
