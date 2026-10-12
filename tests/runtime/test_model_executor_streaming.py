import asyncio
from typing import Any

import pytest
from litellm.exceptions import (
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    TokenCount,
)
from cairn.core.events import Event
from cairn.core.llm import LLMStreamChunk, StreamProtocolError, ToolCallDelta
from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.core.models import LLMUsage, Message
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor, StreamObserverError
from tests.support.runtime import TEST_BUDGET, RecordingSink
from tests.support.streaming import ScriptedStreamLLM


class Counter:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.messages: list[list[Message]] = []

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        self.messages.append(list(messages))
        return TokenCount(self.tokens, False)


def configure(
    first: ScriptedStreamLLM,
    second: ScriptedStreamLLM,
    events: list[Event],
    second_tokens: int = 20,
) -> tuple[Agent, Counter, Counter]:
    manager = ModelManager(
        ProviderConfig(
            "https://example.test", "TEST_KEY", (ModelConfig("group", ("one", "two")),)
        )
    )
    a, b = Counter(10), Counter(second_tokens)
    budget = ContextBudget(max_tokens=100, response_tokens=20)
    builder_a, builder_b = ContextBuilder(budget, a), ContextBuilder(budget, b)
    executor = ModelExecutor(manager, lambda model: (second, builder_b))

    def observe(event: Event) -> None:
        events.append(event)

    agent = Agent(
        llm=first,
        tools=ToolRegistry(),
        context_builder=builder_a,
        model_executor=executor,
        event_handler=observe,
    )
    return agent, a, b


def throttled() -> RateLimitError:
    return RateLimitError(
        message="provider rate limit", model="one", llm_provider="openai"
    )


def test_partial_fallback_uses_fresh_admission_assembly_and_unknown_total_usage() -> (
    None
):
    first = ScriptedStreamLLM(
        [
            LLMStreamChunk(text="abandoned"),
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "old", "record", "{"),)),
            throttled(),
        ]
    )
    second = ScriptedStreamLLM(
        [
            LLMStreamChunk(text="accepted"),
            LLMStreamChunk(finish_reason="stop"),
            LLMStreamChunk(usage=LLMUsage(input_tokens=17, output_tokens=3)),
        ]
    )
    events: list[Event] = []
    agent, a, b = configure(first, second, events)
    sink = RecordingSink()
    agent.tracer = Tracer(sink)
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "accepted"
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert agent.state.messages[-1].content == "accepted"
    assert agent.state.messages[-1].tool_calls == []
    assert first.closed == second.closed == 1
    assert first.calls == second.calls
    assert a.messages == b.messages
    assert [
        event.data["status"] for event in events if event.type == "model_stream_end"
    ] == ["abandoned", "completed"]
    assert [
        event.data["attempt"] for event in events if event.type == "model_text_delta"
    ] == [1, 2]
    attempts = [span for span in sink.spans if span.name == "llm.generate"]
    assert [span.attributes["context_tokens_after"] for span in attempts] == [10, 20]
    assert [span.attributes["model"] for span in attempts] == ["one", "two"]
    assert attempts[-1].attributes["input_tokens"] == 17
    assert attempts[-1].attributes["output_tokens"] == 3
    finish = next(event for event in events if event.type == "trace_finish")
    assert "usage" not in finish.data
    assert "abandoned" not in repr(sink.spans) and "accepted" not in repr(sink.spans)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("transport"),
        Timeout(message="timeout", model="one", llm_provider="openai"),
        ServiceUnavailableError(
            message="unavailable", model="one", llm_provider="openai"
        ),
        StreamProtocolError("invalid chunk"),
    ],
)
def test_ineligible_stream_failure_does_not_try_another_candidate(
    failure: Exception,
) -> None:
    first = ScriptedStreamLLM([LLMStreamChunk(text="partial"), failure])
    second = ScriptedStreamLLM(
        [LLMStreamChunk(text="unused"), LLMStreamChunk(finish_reason="stop")]
    )
    events: list[Event] = []
    agent, _, b = configure(first, second, events)
    with pytest.raises(type(failure)):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert second.calls == b.messages == []
    assert first.closed == 1
    assert agent.state.messages == []


def test_fallback_candidate_admission_failure_does_not_request_provider() -> None:
    first, second = (
        ScriptedStreamLLM([LLMStreamChunk(text="partial"), throttled()]),
        ScriptedStreamLLM(),
    )
    events: list[Event] = []
    agent, _, b = configure(first, second, events, second_tokens=90)
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert b.messages and not second.calls
    assert first.closed == 1
    assert agent.state.messages == []


@pytest.mark.parametrize(
    "phase", ["model_stream_start", "model_text_delta", "model_stream_end"]
)
def test_callback_quota_exception_cannot_become_provider_fallback(phase: str) -> None:
    first = ScriptedStreamLLM(
        [LLMStreamChunk(text="partial"), LLMStreamChunk(finish_reason="stop")]
    )
    second = ScriptedStreamLLM()
    events: list[Event] = []
    agent, _, b = configure(first, second, events)

    def observer(event: Event) -> None:
        if event.type == phase:
            error = APIError(
                status_code=403,
                message="quota callback",
                model="one",
                llm_provider="openai",
            )
            error.code = "AllocationQuota.FreeTierOnly"
            raise error

    agent.event_handler = observer
    with pytest.raises(StreamObserverError):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert first.closed == (0 if phase == "model_stream_start" else 1)
    assert len(first.calls) == (0 if phase == "model_stream_start" else 1)
    assert second.calls == b.messages == []
    assert agent.state.messages == []


def test_failed_abandonment_notification_preserves_primary_error_and_stops_fallback() -> (
    None
):
    primary = throttled()
    first = ScriptedStreamLLM([LLMStreamChunk(text="partial"), primary])
    second = ScriptedStreamLLM()
    events: list[Event] = []
    agent, _, b = configure(first, second, events)

    def observer(event: Event) -> None:
        if event.type == "model_stream_end":
            raise RuntimeError("observer failed")

    agent.event_handler = observer
    with pytest.raises(RateLimitError) as error:
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert error.value is primary
    assert first.closed == 1 and second.calls == b.messages == []
    assert agent.state.messages == []
