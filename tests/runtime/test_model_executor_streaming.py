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
from cairn.core.models import LLMUsage, Message, ToolCall, ToolResult
from cairn.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionResult,
)
from cairn.infra.llm import litellm_client as client_module
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor, StreamObserverError
from tests.support.runtime import TEST_BUDGET, NetworkRequestTool, RecordingSink
from tests.support.streaming import (
    CompletionResponse,
    ProviderStream,
    ScriptedStreamLLM,
    provider_chunk,
)


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


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-3-5-sonnet-latest",
        "deepseek/deepseek-chat",
        "gemini/gemini-2.0-flash",
        "azure_ai/model",
        "gpt-4o",
        "openai/responses/gpt-4o",
        "text-completion-openai/text-davinci-003",
    ],
)
def test_unsupported_stream_configuration_uses_one_generate_request(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> CompletionResponse:
        requests.append(kwargs)
        assert "stream" not in kwargs, "Unsupported route must select generate first"
        return CompletionResponse(
            choices=[
                CompletionResponse(
                    message=CompletionResponse(content="done", tool_calls=[])
                )
            ],
            usage=CompletionResponse(prompt_tokens=17, completion_tokens=3),
        )

    monkeypatch.setattr(client_module, "acompletion", completion)
    events: list[Event] = []
    sink = RecordingSink()
    agent = Agent(
        llm=LiteLLMClient(model, max_output_tokens=42),
        tools=ToolRegistry(),
        event_handler=lambda event: events.append(event),
        tracer=Tracer(sink),
    )
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"
    assert len(requests) == 1
    assert requests[0]["model"] == model
    assert requests[0]["max_tokens"] == 42
    assert "stream_options" not in requests[0]
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert agent.state.messages[-1].content == "done"
    assert not any(event.type.startswith("model_") for event in events)
    attempt = next(span for span in sink.spans if span.name == "llm.generate")
    assert attempt.attributes["input_tokens"] == 17
    assert attempt.attributes["output_tokens"] == 3
    finish = next(event for event in events if event.type == "trace_finish")
    assert finish.data["usage"] == {"input_tokens": 17, "output_tokens": 3}


@pytest.mark.parametrize(
    "models",
    [
        ("openai/one", "anthropic/two"),
        ("anthropic/one", "openai/two"),
    ],
)
def test_fallback_selects_streaming_per_candidate_with_matching_admission(
    monkeypatch: pytest.MonkeyPatch, models: tuple[str, str]
) -> None:
    requests: list[dict[str, Any]] = []
    failed_stream: ProviderStream | None = None

    async def completion(**kwargs: Any) -> object:
        nonlocal failed_stream
        requests.append(kwargs)
        model = kwargs["model"]
        assert kwargs.get("stream", False) is model.startswith("openai/")
        if model == models[0]:
            error = RateLimitError(
                message="provider rate limit",
                model=model,
                llm_provider=model.split("/")[0],
            )
            if kwargs.get("stream"):
                failed_stream = ProviderStream(provider_chunk("abandoned"), error)
                return failed_stream
            raise error
        return CompletionResponse(
            choices=[
                CompletionResponse(
                    message=CompletionResponse(content="accepted", tool_calls=[])
                )
            ],
            usage=CompletionResponse(prompt_tokens=17, completion_tokens=3),
        )

    monkeypatch.setattr(client_module, "acompletion", completion)
    counters = Counter(10), Counter(20)
    builders = tuple(
        ContextBuilder(ContextBudget(max_tokens=100, response_tokens=reserve), counter)
        for reserve, counter in zip((20, 30), counters, strict=True)
    )
    clients = tuple(
        LiteLLMClient(model, api_base="https://example.test", max_output_tokens=reserve)
        for model, reserve in zip(models, (20, 30), strict=True)
    )
    manager = ModelManager(
        ProviderConfig(
            "https://example.test", "TEST_KEY", (ModelConfig("group", models),)
        )
    )
    built: list[str] = []

    def runtime(model: str) -> tuple[LiteLLMClient, ContextBuilder]:
        built.append(model)
        assert model == models[1]
        return clients[1], builders[1]

    events: list[Event] = []
    sink = RecordingSink()
    agent = Agent(
        llm=clients[0],
        context_builder=builders[0],
        model_executor=ModelExecutor(manager, runtime),
        tools=ToolRegistry(),
        event_handler=lambda event: events.append(event),
        tracer=Tracer(sink),
    )
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "accepted"
    assert [request["model"] for request in requests] == list(models)
    assert [request["max_tokens"] for request in requests] == [20, 30]
    assert built == [models[1]]
    assert counters[0].messages == counters[1].messages
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]
    assert agent.state.messages[-1].content == "accepted"
    attempts = [span for span in sink.spans if span.name == "llm.generate"]
    assert [span.attributes["model"] for span in attempts] == list(models)
    assert [span.attributes["context_tokens_after"] for span in attempts] == [10, 20]
    assert attempts[1].attributes["input_tokens"] == 17
    assert attempts[1].attributes["output_tokens"] == 3
    statuses = [
        event.data["status"] for event in events if event.type == "model_stream_end"
    ]
    assert statuses == (
        ["abandoned"] if models[0].startswith("openai/") else ["completed"]
    )
    if failed_stream is not None:
        assert failed_stream.closed
    finish = next(event for event in events if event.type == "trace_finish")
    assert "usage" not in finish.data


def test_unexpected_stream_shape_does_not_retry_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    stream = ProviderStream({"choices": [{"index": 0}, {"index": 1}]})

    async def completion(**kwargs: Any) -> ProviderStream:
        requests.append(kwargs)
        assert kwargs["stream"] is True
        return stream

    monkeypatch.setattr(client_module, "acompletion", completion)
    agent = Agent(llm=LiteLLMClient("openai/test"), tools=ToolRegistry())
    with pytest.raises(StreamProtocolError):
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET))
    assert len(requests) == 1
    assert stream.closed
    assert agent.state.messages == []


@pytest.mark.parametrize("allowed", [False, True])
def test_generate_selected_route_keeps_tool_permission_and_state_semantics(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
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
            assert context is not None and context.network_access
            order.append("execute")
            return await super().execute(arguments, context=context)

    responses = iter(
        [
            CompletionResponse(
                choices=[
                    CompletionResponse(
                        message=CompletionResponse(
                            content=None,
                            tool_calls=[
                                CompletionResponse(
                                    id="call",
                                    function=CompletionResponse(
                                        name="bash",
                                        arguments='{"command":"curl example.com","network_access":true,"justification":"fetch"}',
                                    ),
                                )
                            ],
                        )
                    )
                ]
            ),
            CompletionResponse(
                choices=[
                    CompletionResponse(
                        message=CompletionResponse(content="done", tool_calls=[])
                    )
                ]
            ),
        ]
    )
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> CompletionResponse:
        requests.append(kwargs)
        assert "stream" not in kwargs
        return next(responses)

    monkeypatch.setattr(client_module, "acompletion", completion)
    tool = CheckedTool()
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []
    agent = Agent(
        llm=LiteLLMClient("anthropic/test"),
        tools=registry,
        event_handler=lambda event: events.append(event),
    )

    def approve(tool_call: ToolCall) -> PermissionResult:
        order.append("permission")
        assert tool.calls == []
        assert agent.state.messages[-1].tool_calls == [tool_call]
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=allowed,
            granted_capabilities=frozenset({PermissionCapability.NETWORK})
            if allowed
            else frozenset(),
        )

    agent.permission_handler = approve
    assert asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET)) == "done"
    assert len(requests) == 2  # One request per accepted Agent step.
    assert order == (
        ["preflight", "permission", "execute"]
        if allowed
        else ["preflight", "permission"]
    )
    assert len(tool.calls) == int(allowed)
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert agent.state.messages[2].tool_call_id == "call"
    assert any(event.type == "tool_denied" for event in events) is (not allowed)


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
