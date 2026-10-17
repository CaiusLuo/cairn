import asyncio
import dataclasses
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import cairn.runtime.provider_runtime as provider_module
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.models import Message
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.llm.token_counter import LiteLLMTokenCounter
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.provider_runtime import (
    ProviderRuntime,
    ProviderSession,
    build_provider_runtime,
)
from cairn.runtime.session import Session

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


@pytest.mark.parametrize("initial", [True, False], ids=["activation", "switch"])
def test_cancelled_provider_commit_preserves_bindings_and_session(
    monkeypatch: pytest.MonkeyPatch, initial: bool
) -> None:
    """A caller's cancellation fence refuses activation before binding changes."""
    previous = build_provider_runtime(make_provider(), "first", BUDGET)
    agent = Agent(
        previous.llm,
        ToolRegistry(),
        context_builder=previous.context_builder,
        model_executor=previous.model_executor,
    )
    session = Session(agent)
    agent.state.messages.append(Message(role="user", content="keep history"))
    history = agent.state.messages.copy()
    permissions = SessionPermissionHandler()
    observed: list[ProviderRuntime] = []

    def commit(runtime: ProviderRuntime) -> None:
        observed.append(runtime)
        permissions.reset_grants()

    cancelled = threading.Event()
    cancelled.set()

    @contextmanager
    def cancelled_guard() -> Iterator[None]:
        if cancelled.is_set():
            raise asyncio.CancelledError
        yield

    bindings = (
        None if initial else ProviderSession(previous, agent=agent, on_commit=commit)
    )
    observed.clear()
    permissions.grants.add(PermissionCapability.NETWORK)
    with pytest.raises(asyncio.CancelledError):
        if bindings is None:
            ProviderSession(
                previous, agent=agent, on_commit=commit, commit_guard=cancelled_guard
            )
        else:
            candidate = build_provider_runtime(make_provider("other"), "second", BUDGET)
            monkeypatch.setattr(
                provider_module, "build_provider_runtime", lambda *a, **kw: candidate
            )
            bindings.select_provider(
                "other",
                ProviderCatalog((previous.provider, candidate.provider)),
                confirm=lambda p, m: True,
                resolve_credential=lambda p: "second",
                context_budget=BUDGET,
                commit_guard=cancelled_guard,
            )
    assert agent.llm is previous.llm
    assert agent.context_builder is previous.context_builder
    assert agent.model_executor is previous.model_executor
    assert agent.session_owner is session
    assert agent.state.messages == history
    assert permissions.grants == {PermissionCapability.NETWORK}
    assert observed == []


def test_provider_cancellation_after_commit_keeps_new_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once committed, late cancellation cannot restore an old runtime."""
    previous = build_provider_runtime(make_provider(), "first", BUDGET)
    candidate = build_provider_runtime(make_provider("other"), "second", BUDGET)
    agent = Agent(
        previous.llm,
        ToolRegistry(),
        context_builder=previous.context_builder,
        model_executor=previous.model_executor,
    )
    session = Session(agent)
    agent.state.messages.append(Message(role="user", content="keep history"))
    history = agent.state.messages.copy()
    observed: list[ProviderRuntime] = []

    def commit(runtime: ProviderRuntime) -> None:
        observed.append(runtime)

    bindings = ProviderSession(previous, agent=agent, on_commit=commit)
    monkeypatch.setattr(
        provider_module, "build_provider_runtime", lambda *a, **kw: candidate
    )

    @contextmanager
    def late_cancel_guard() -> Iterator[None]:
        yield
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        bindings.select_provider(
            "other",
            ProviderCatalog((previous.provider, candidate.provider)),
            confirm=lambda p, m: True,
            resolve_credential=lambda p: "second",
            context_budget=BUDGET,
            commit_guard=late_cancel_guard,
        )
    assert agent.llm is candidate.llm
    assert agent.context_builder is candidate.context_builder
    assert agent.model_executor is candidate.model_executor
    assert agent.provider_name == candidate.provider.name
    assert agent.session_owner is session
    assert agent.state.messages == history
    assert observed == [previous, candidate]
