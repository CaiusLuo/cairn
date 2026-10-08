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
from cairn.llm.base import LLMClient
from cairn.llm.model_executor import (
    AllModelsUnavailable,
    ModelExecutor,
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


def make_executor(agent: Agent, manager: ModelManager) -> ModelExecutor:
    # These tests isolate retry control with scripted outcomes. Model-specific
    # client/counter binding is covered separately through the CLI request path.
    return ModelExecutor(manager, lambda _model: (agent.llm, agent.context_builder))


def test_executor_rejects_a_partial_runtime_configuration() -> None:
    agent = make_agent(ScriptedLLM(), RecordingCounter())

    with pytest.raises(ValueError, match="must be supplied together"):
        ModelExecutor(make_manager())
    with pytest.raises(ValueError, match="must be supplied together"):
        ModelExecutor(runtime_factory=lambda _model: (agent.llm, agent.context_builder))
    assert ModelExecutor().manager is None


def rate_limit() -> RateLimitError:
    return RateLimitError(
        message="model request limit", model="openai/flash", llm_provider="openai"
    )


class ProviderPayloadError(Exception):
    """Stands in for the provider exception LiteLLM chains its rebuild onto."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__("provider payload")
        self.body = body


def rate_limit_with_provider_payload(body: dict[str, Any]) -> RateLimitError:
    """Mimic LiteLLM 1.101: the rebuilt error drops the provider's payload."""
    error = rate_limit()
    error.__context__ = ProviderPayloadError(body)
    return error


def test_fallback_accepts_an_unclassified_vendor_rate_limit() -> None:
    error = rate_limit()

    # LiteLLM's SDK path leaves `body` empty and `rate_limit_type` unset.
    assert error.category == "vendor_rate_limit"
    assert error.rate_limit_type is None
    assert error.body is None
    assert is_fallback_error(error)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "rate_limit_exceeded", "type": "rate_limit_exceeded"}},
        {"code": "Throttling", "type": "Throttling.AllocationQuota"},
    ],
)
def test_fallback_accepts_model_level_provider_payloads(
    payload: dict[str, Any],
) -> None:
    assert is_fallback_error(rate_limit_with_provider_payload(payload))


@pytest.mark.parametrize(
    "error",
    [
        Timeout(message="timed out", model="openai/flash", llm_provider="openai"),
        TimeoutError("request deadline expired"),
        ServiceUnavailableError(
            message="unavailable", model="openai/flash", llm_provider="openai"
        ),
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
def test_fallback_does_not_guess_from_messages_or_swallow_other_failures(
    error: BaseException,
) -> None:
    assert not is_fallback_error(error)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("category", "litellm_rate_limit"),
        ("category", "litellm_batch_rate_limit"),
        ("rate_limit_type", "budget"),
        ("rate_limit_type", "max_iterations"),
    ],
)
def test_litellm_side_limits_do_not_trigger_fallback(
    attribute: str, value: str
) -> None:
    error = rate_limit()
    setattr(error, attribute, value)

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
    "body",
    [
        {"error": {"code": "insufficient_quota", "type": "insufficient_quota"}},
        {"error": {"type": "billing_hard_limit_reached"}},
    ],
)
def test_shared_quota_on_the_chained_provider_error_is_not_a_model_fallback(
    body: dict[str, Any],
) -> None:
    error = rate_limit_with_provider_payload(body)

    assert error.body is None
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


def test_executor_reuses_the_selected_runtime_and_builds_only_fallback_runtimes() -> (
    None
):
    selected_llm = ScriptedLLM(rate_limit())
    fallback_llm = ScriptedLLM(LLMResponse(content="answer"))
    fallback_counter = RecordingCounter(tokens=30)
    built: list[tuple[ModelConfig, LLMClient]] = []
    agent = make_agent(selected_llm, RecordingCounter(tokens=10))
    manager = make_manager()

    def runtime_factory(model: ModelConfig) -> tuple[LLMClient, ContextBuilder]:
        built.append((model, fallback_llm))
        return fallback_llm, ContextBuilder(
            budget=ContextBudget(max_tokens=100, response_tokens=20),
            counter=fallback_counter,
        )

    executor = ModelExecutor(manager, runtime_factory)
    _, response = asyncio.run(
        executor.execute(
            agent=agent,
            system_messages=[],
            history=agent.state.messages,
            current_turn_start=0,
            tools=[],
        )
    )

    assert response.content == "answer"
    assert [model.name for model, _ in built] == ["plus"]
    assert len(selected_llm.calls) == 1
    assert len(fallback_llm.calls) == 1
    # The fallback attempt was admitted with, and sent through, its own runtime.
    assert fallback_counter.calls == fallback_llm.calls


def test_executor_never_builds_a_runtime_for_the_selected_model() -> None:
    response = LLMResponse(content="answer")
    agent = make_agent(ScriptedLLM(response), RecordingCounter())
    manager = make_manager()

    def unexpected_runtime(model: ModelConfig) -> tuple[LLMClient, ContextBuilder]:
        raise AssertionError(f"selected model {model.name} rebuilt its runtime")

    _, actual = asyncio.run(
        ModelExecutor(manager, unexpected_runtime).execute(
            agent=agent,
            system_messages=[],
            history=agent.state.messages,
            current_turn_start=0,
            tools=[],
        )
    )

    assert actual is response


def test_executor_records_the_failure_and_clears_it_after_a_success() -> None:
    outcomes: list[LLMResponse | BaseException] = [
        rate_limit(),
        LLMResponse(content="plus answered"),
        LLMResponse(content="flash answered"),
    ]
    llm = ScriptedLLM(*outcomes)
    agent = make_agent(llm, RecordingCounter())
    executor = make_executor(agent, make_manager())

    _, first = asyncio.run(
        executor.execute(
            agent=agent,
            system_messages=[],
            history=agent.state.messages,
            current_turn_start=0,
            tools=[],
        )
    )

    assert first.content == "plus answered"
    flash_failure = executor.failure_for("flash")
    assert flash_failure is not None
    assert flash_failure.category == "RateLimitError"
    assert "model request limit" in flash_failure.detail
    assert executor.failure_for("plus") is None

    _, second = asyncio.run(
        executor.execute(
            agent=agent,
            system_messages=[],
            history=agent.state.messages,
            current_turn_start=0,
            tools=[],
        )
    )

    assert second.content == "flash answered"
    assert executor.failure_for("flash") is None


def test_executor_failure_detail_stays_short_and_single_line() -> None:
    llm = ScriptedLLM(RuntimeError("line one\nline two " + "x" * 500))
    agent = make_agent(llm, RecordingCounter())
    executor = make_executor(agent, make_manager())

    with pytest.raises(RuntimeError):
        asyncio.run(
            executor.execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        )

    failure = executor.failure_for("flash")
    assert failure is not None
    assert failure.category == "RuntimeError"
    assert len(failure.detail) <= 80
    assert "\n" not in failure.detail


def test_executor_aggregates_attempts_and_restarts_the_order_for_a_new_request() -> (
    None
):
    failures = [rate_limit(), rate_limit()]
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
        assert executor.failure_for("flash") is not None
        assert executor.failure_for("plus") is not None

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
    "failure",
    [
        ValueError("invalid request"),
        AuthenticationError(
            message="invalid key", model="openai/flash", llm_provider="openai"
        ),
        ServiceUnavailableError(
            message="unavailable", model="openai/flash", llm_provider="openai"
        ),
        Timeout(message="timed out", model="openai/flash", llm_provider="openai"),
        asyncio.CancelledError(),
    ],
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


def test_executor_admission_failure_on_a_fallback_never_calls_that_provider() -> None:
    selected_llm = ScriptedLLM(rate_limit())
    fallback_llm = ScriptedLLM(LLMResponse(content="must not run"))
    agent = make_agent(selected_llm, RecordingCounter(tokens=10))
    manager = make_manager()

    def runtime_factory(model: ModelConfig) -> tuple[LLMClient, ContextBuilder]:
        return fallback_llm, ContextBuilder(
            budget=ContextBudget(max_tokens=100, response_tokens=20),
            counter=RecordingCounter(tokens=90),
        )

    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(
            ModelExecutor(manager, runtime_factory).execute(
                agent=agent,
                system_messages=[],
                history=agent.state.messages,
                current_turn_start=0,
                tools=[],
            )
        )

    assert len(selected_llm.calls) == 1
    assert fallback_llm.calls == []
