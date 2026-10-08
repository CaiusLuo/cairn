import asyncio
from typing import Any

import httpx
import pytest
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    ContextWindowExceededError,
    PermissionDeniedError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from cairn.core.agent import Agent
from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    TokenCount,
)
from cairn.core.models import LLMResponse, LLMUsage, Message
from cairn.llm.model_executor import (
    AllModelsUnavailable,
    ModelExecuter,
    is_fallback_error,
)
from cairn.llm.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.tools.registry import ToolRegistry


class ScriptedLLM:
    """Fake provider outcomes; this does not implement candidate runtime selection."""

    def __init__(self, *outcomes: LLMResponse | BaseException) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[list[Message], list[dict[str, Any]] | None]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.calls.append((list(messages), tools))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingCounter:
    def __init__(self, tokens: int = 10) -> None:
        self.tokens = tokens
        self.calls: list[tuple[list[Message], list[dict[str, Any]]]] = []

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        self.calls.append((list(messages), tools))
        return TokenCount(tokens=self.tokens, is_estimate=False)


def make_manager() -> ModelManager:
    return ModelManager(
        ProviderConfig(
            base_url="https://example.test/v1",
            api_key_env="TEST_KEY",
            model_config=(
                ModelConfig("flash", "openai/flash"),
                ModelConfig("plus", "openai/plus"),
            ),
        )
    )


def make_agent(llm: ScriptedLLM, counter: RecordingCounter) -> Agent:
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        context_builder=ContextBuilder(
            budget=ContextBudget(max_tokens=100, response_tokens=20), counter=counter
        ),
    )
    agent.state.add_user_message("hello")
    return agent


def make_executor(agent: Agent, manager: ModelManager) -> ModelExecuter:
    # These tests isolate retry control with scripted outcomes. Model-specific
    # client/counter binding is covered separately through the CLI request path.
    return ModelExecuter(manager, lambda _model: (agent.llm, agent.context_builder))


def rate_limit() -> RateLimitError:
    return RateLimitError(
        message="model request limit", model="openai/flash", llm_provider="openai"
    )


@pytest.mark.parametrize(
    "error",
    [
        rate_limit(),
        Timeout(message="timed out", model="openai/flash", llm_provider="openai"),
        TimeoutError("request deadline expired"),
        ServiceUnavailableError(
            message="unavailable", model="openai/flash", llm_provider="openai"
        ),
    ],
)
def test_fallback_accepts_typed_provider_limits_timeouts_and_unavailability(
    error: Exception,
) -> None:
    assert is_fallback_error(error)


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError(
            message="invalid key", model="openai/flash", llm_provider="openai"
        ),
        PermissionDeniedError(
            message="forbidden",
            model="openai/flash",
            llm_provider="openai",
            response=httpx.Response(
                403, request=httpx.Request("POST", "https://example.test/v1")
            ),
        ),
        BadRequestError(
            message="bad tool schema", model="openai/flash", llm_provider="openai"
        ),
        ContextWindowExceededError(
            message="too long", model="openai/flash", llm_provider="openai"
        ),
        APIConnectionError(
            message="unknown network failure",
            model="openai/flash",
            llm_provider="openai",
        ),
        APIError(
            status_code=500,
            message="unclassified",
            model="openai/flash",
            llm_provider="openai",
        ),
        ValueError("quota exceeded, 429, timeout, service unavailable"),
        RuntimeError("unclassified provider failure"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ],
)
def test_fallback_does_not_guess_from_messages_or_swallow_permanent_errors(
    error: BaseException,
) -> None:
    assert not is_fallback_error(error)


@pytest.mark.parametrize(
    "details",
    [
        {"code": "insufficient_quota"},
        {"error": {"type": "billing_hard_limit_reached"}},
    ],
)
def test_shared_quota_body_is_not_a_model_fallback(details: dict[str, Any]) -> None:
    error = rate_limit()
    error.body = details

    assert not is_fallback_error(error)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("code", "insufficient_quota"),
        ("rate_limit_type", "budget"),
        ("category", "litellm_rate_limit"),
    ],
)
def test_shared_billing_and_proxy_limits_do_not_trigger_fallback(
    attribute: str, value: str
) -> None:
    error = rate_limit()
    setattr(error, attribute, value)

    assert not is_fallback_error(error)


def test_all_unavailable_snapshots_ordered_causes_without_printing_provider_secrets() -> (
    None
):
    secret = "test-only-secret"
    first = rate_limit()
    second = TimeoutError(f"Authorization: Bearer {secret}")
    failures: list[tuple[str, Exception]] = [("flash", first), ("plus", second)]

    error = AllModelsUnavailable(failures)
    failures.clear()

    assert error.errors == (("flash", first), ("plus", second))
    assert str(error).index("flash") < str(error).index("plus")
    assert "RateLimitError" in str(error)
    assert "TimeoutError" in str(error)
    assert secret not in str(error) + repr(error)
    assert str(AllModelsUnavailable([])) == "No candidate models are available."


@pytest.mark.parametrize("fails_first", [False, True])
def test_executor_returns_built_request_and_response_without_mutating_history(
    fails_first: bool,
) -> None:
    response = LLMResponse(
        content="done", usage=LLMUsage(input_tokens=10, output_tokens=2)
    )
    outcomes: list[LLMResponse | BaseException] = (
        [rate_limit(), response] if fails_first else [response]
    )
    llm = ScriptedLLM(*outcomes)
    counter = RecordingCounter()
    agent = make_agent(llm, counter)
    history = agent.state.messages
    before = list(history)
    manager = make_manager()
    tools = [{"type": "function", "function": {"name": "record"}}]
    system = [Message(role="system", content="system")]

    request, actual = asyncio.run(
        make_executor(agent, manager).execute(
            agent=agent,
            system_messages=system,
            history=history,
            current_turn_start=0,
            tools=tools,
        )
    )

    assert actual is response
    assert request.messages == [*system, *before]
    assert request.tokens_after == TokenCount(tokens=10, is_estimate=False)
    assert len(llm.calls) == (2 if fails_first else 1)
    assert counter.calls == llm.calls
    assert agent.state.messages is history
    assert history == before
    assert manager.current_model().name == "flash"


def test_executor_aggregates_attempts_and_restarts_the_order_for_a_new_request() -> (
    None
):
    failures = [rate_limit(), TimeoutError("provider deadline")]
    response = LLMResponse(content="next request works")
    llm = ScriptedLLM(*failures, response)
    agent = make_agent(llm, RecordingCounter())
    manager = make_manager()
    executor = make_executor(agent, manager)

    async def scenario() -> None:
        with pytest.raises(AllModelsUnavailable) as raised:
            await executor.execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        assert raised.value.errors == (("flash", failures[0]), ("plus", failures[1]))
        assert raised.value.__cause__ is None
        assert manager.current_model().name == "flash"
        assert [m.content for m in agent.state.messages] == ["hello"]

        _, actual = await executor.execute(
            agent=agent,
            system_messages=[],
            history=agent.state.messages,
            current_turn_start=0,
            tools=[],
        )
        assert actual is response

    asyncio.run(scenario())
    assert len(llm.calls) == 3


def test_executor_only_attempts_candidates_at_or_after_manual_selection() -> None:
    manager = make_manager()
    manager.select_model("plus")
    failure = rate_limit()
    llm = ScriptedLLM(failure)
    agent = make_agent(llm, RecordingCounter())

    with pytest.raises(AllModelsUnavailable) as raised:
        asyncio.run(
            make_executor(agent, manager).execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        )

    assert raised.value.errors == (("plus", failure),)
    assert len(llm.calls) == 1
    assert manager.current_model().name == "plus"


@pytest.mark.parametrize(
    "failure", [ValueError("invalid request"), asyncio.CancelledError()]
)
def test_executor_propagates_non_fallback_and_cancellation_without_retry(
    failure: BaseException,
) -> None:
    llm = ScriptedLLM(failure, LLMResponse(content="must not run"))
    agent = make_agent(llm, RecordingCounter())

    with pytest.raises(type(failure)) as raised:
        asyncio.run(
            make_executor(agent, make_manager()).execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        )

    assert raised.value is failure
    assert len(llm.calls) == 1
    assert [m.content for m in agent.state.messages] == ["hello"]


def test_executor_context_admission_failure_never_calls_provider() -> None:
    llm = ScriptedLLM(LLMResponse(content="must not run"))
    agent = make_agent(llm, RecordingCounter(tokens=90))

    with pytest.raises(ContextBudgetExceeded) as raised:
        asyncio.run(
            make_executor(agent, make_manager()).execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        )

    assert not is_fallback_error(raised.value)
    assert llm.calls == []
    assert [m.content for m in agent.state.messages] == ["hello"]
