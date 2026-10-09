import dataclasses

import pytest

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.llm.litellm_client import LiteLLMClient
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider
from cairn.llm.provider_runtime import ProviderRuntime, build_provider_runtime
from cairn.llm.token_counter import LiteLLMTokenCounter

BUDGET = ContextBudget(max_tokens=100, response_tokens=20)


def make_provider(
    name: str = "bailian",
    *,
    base_url: str = "https://example.test/v1",
) -> NamedProvider:
    return NamedProvider(
        name=name,
        config=ProviderConfig(
            base_url=base_url,
            api_key_env=f"{name.upper()}_API_KEY",
            model_config=(
                ModelConfig("flash", ("openai/model-a", "openai/model-b")),
                ModelConfig("plus", ("openai/model-c",)),
            ),
        ),
    )


def test_runtime_binds_provider_endpoint_credential_models_and_budget() -> None:
    provider = make_provider()

    runtime = build_provider_runtime(provider, "credential", BUDGET)

    assert runtime.provider is provider
    assert runtime.model_manager.config is provider.config
    assert runtime.model_manager.current_model().name == "flash"
    assert isinstance(runtime.llm, LiteLLMClient)
    assert (runtime.llm.model, runtime.llm.api_base, runtime.llm.api_key) == (
        "openai/model-a",
        "https://example.test/v1",
        "credential",
    )
    assert runtime.llm.max_output_tokens == BUDGET.response_tokens
    assert runtime.context_builder.budget == BUDGET
    counter = runtime.context_builder.counter
    assert isinstance(counter, LiteLLMTokenCounter)
    assert counter.model == "openai/model-a"
    assert runtime.model_executor.manager is runtime.model_manager
    assert runtime.model_executor.runtime_factory is runtime.runtime_factory


def test_runtime_can_start_on_a_named_group() -> None:
    provider = make_provider()

    runtime = build_provider_runtime(provider, "credential", BUDGET, group="plus")

    assert runtime.model_manager.current_model().name == "plus"
    assert isinstance(runtime.llm, LiteLLMClient)
    assert runtime.llm.model == "openai/model-c"
    counter = runtime.context_builder.counter
    assert isinstance(counter, LiteLLMTokenCounter)
    assert counter.model == "openai/model-c"


def test_runtime_rejects_an_unknown_group() -> None:
    with pytest.raises(ValueError, match="not found"):
        build_provider_runtime(make_provider(), "credential", BUDGET, group="absent")


def test_each_factory_stays_bound_to_its_own_provider() -> None:
    first = build_provider_runtime(make_provider("bailian"), "first-credential", BUDGET)
    second = build_provider_runtime(
        make_provider("local", base_url="http://localhost:8000/v1"),
        "second-credential",
        BUDGET,
    )

    first_llm, first_context = first.runtime_factory("openai/other")
    second_llm, second_context = second.runtime_factory("openai/other")

    assert isinstance(first_llm, LiteLLMClient)
    assert isinstance(second_llm, LiteLLMClient)
    assert (first_llm.api_base, first_llm.api_key) == (
        "https://example.test/v1",
        "first-credential",
    )
    assert (second_llm.api_base, second_llm.api_key) == (
        "http://localhost:8000/v1",
        "second-credential",
    )
    assert first.model_executor is not second.model_executor
    assert isinstance(first_context, ContextBuilder)
    first_counter = first_context.counter
    second_counter = second_context.counter
    assert isinstance(first_counter, LiteLLMTokenCounter)
    assert isinstance(second_counter, LiteLLMTokenCounter)
    assert first_counter.model == second_counter.model == "openai/other"


def test_provider_runtime_is_immutable() -> None:
    runtime: ProviderRuntime = build_provider_runtime(
        make_provider(), "credential", BUDGET
    )

    # A frozen dataclass rejects even a same-value assignment.
    with pytest.raises(dataclasses.FrozenInstanceError):
        runtime.llm = runtime.llm  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        runtime.provider = make_provider("other")  # type: ignore[misc]
