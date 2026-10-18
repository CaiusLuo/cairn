"""One provider's fully constructed runtime, bound to its endpoint and credential.

Every provider owns its model manager, executor, runtime factory, selected
client and context builder. A switch builds the target's ``ProviderRuntime``
completely before anything in the active session is rebound, so denied
approval, a missing credential or a construction failure leaves the active
provider untouched. :class:`ProviderSession` owns that ordering and the
rebinding it performs.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.llm import LLMClient
from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog
from cairn.runtime.model_executor import ModelExecutor

if TYPE_CHECKING:
    from cairn.runtime.agent import Agent

#: Builds the client and context builder for one concrete model ID.
RuntimeFactory = Callable[[str], tuple[LLMClient, ContextBuilder]]


@dataclass(frozen=True)
class ProviderRuntime:
    """A provider's runtime, ready to serve its current model group."""

    provider: NamedProvider
    model_manager: ModelManager
    model_executor: ModelExecutor
    llm: LLMClient
    context_builder: ContextBuilder
    runtime_factory: RuntimeFactory


def build_provider_runtime(
    provider: NamedProvider,
    credential: str,
    context_budget: ContextBudget,
    *,
    group: str | None = None,
) -> ProviderRuntime:
    """Construct one provider's runtime without touching any other provider.

    Only the credential resolved by the caller after approval is used. The
    provider's endpoint, output-token allowance and selected model group are
    bound into the runtime and into every runtime this provider's factory
    creates later, so fallback candidates never leave the active provider.
    """
    # Keep the provider SDK out of the core/CLI import path until needed.
    from cairn.infra.llm.litellm_client import LiteLLMClient
    from cairn.infra.llm.token_counter import LiteLLMTokenCounter

    def create_model_runtime(model_id: str) -> tuple[LLMClient, ContextBuilder]:
        return (
            LiteLLMClient(
                model=model_id,
                api_key=credential,
                api_base=provider.config.base_url,
                max_output_tokens=context_budget.response_tokens,
            ),
            ContextBuilder(
                budget=context_budget,
                counter=LiteLLMTokenCounter(model_id),
            ),
        )

    model_manager = ModelManager(provider.config)
    if group is not None:
        model_manager.select_model(group)
    llm, context_builder = create_model_runtime(
        model_manager.current_model().model_ids[0]
    )

    return ProviderRuntime(
        provider=provider,
        model_manager=model_manager,
        model_executor=ModelExecutor(model_manager, create_model_runtime),
        llm=llm,
        context_builder=context_builder,
        runtime_factory=create_model_runtime,
    )


class UnknownModelError(ValueError):
    """A requested model group is not configured for the active provider."""


def find_model(manager: ModelManager, name: str) -> ModelConfig:
    """Return the named model group, or raise :class:`UnknownModelError`.

    This is the session's single name-to-group lookup. Callers resolve a group
    before building its runtime, so a failed build never changes the selection.
    """
    for model in manager.list_models():
        if model.name == name:
            return model
    raise UnknownModelError(f"Model {name!r} not found in the configuration.")


class ProviderSession:
    """Owns the active provider runtime and every rebinding that commits one.

    :meth:`commit` is the only place that rebinds the Agent's provider
    bindings, and callers must build the target runtime completely before
    committing it. State the runtime does not own (the terminal's command
    context) observes commits through ``on_commit``.
    """

    def __init__(
        self,
        runtime: ProviderRuntime,
        *,
        agent: "Agent",
        on_commit: Callable[[ProviderRuntime], None] | None = None,
        commit_guard: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self._agent = agent
        self._on_commit = on_commit
        with commit_guard():
            self.commit(runtime)

    def commit(self, runtime: ProviderRuntime) -> None:
        """Bind one fully built runtime to the session's holders."""
        self._runtime = runtime
        agent = self._agent
        agent.llm = runtime.llm
        agent.context_builder = runtime.context_builder
        agent.model_executor = runtime.model_executor
        agent.provider_name = runtime.provider.name
        if self._on_commit is not None:
            self._on_commit(runtime)

    def select_model(self, name: str) -> ModelConfig:
        """Select one of the active provider's model groups for later turns.

        The replacement client and context builder are built before the
        selection changes, so a construction failure leaves the current group,
        client and token counter in place.
        """
        runtime = self._runtime
        manager = runtime.model_manager
        previous = manager.current_model()
        if name == previous.name:
            return previous
        selected = find_model(manager, name)
        next_llm, next_context = runtime.runtime_factory(selected.model_ids[0])
        manager.select_model(name)
        # No await separates the pair, and the existing Agent (state, tools,
        # permissions and tracer) is kept.
        self.commit(replace(runtime, llm=next_llm, context_builder=next_context))
        return selected

    def select_provider(
        self,
        name: str,
        catalog: ProviderCatalog,
        *,
        confirm: Callable[[NamedProvider, ModelConfig], bool],
        resolve_credential: Callable[[ProviderConfig], str],
        context_budget: ContextBudget,
        commit_guard: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> bool:
        """Switch this session to another configured provider.

        ``confirm`` and ``resolve_credential`` are supplied by the caller, which
        keeps prompting and credential resolution at the presentation boundary.
        The target is built completely before :meth:`commit`, so denied
        approval, a missing credential or a construction failure leaves the
        current provider usable. Returns ``True`` when a switch committed and
        ``False`` when ``name`` was already active.

        A caller's optional ``commit_guard`` linearizes cancellation with the
        binding change. Only :meth:`commit` runs inside it; approval, credential
        resolution and construction remain outside that critical section.
        """
        runtime = self._runtime
        target = catalog.by_name(name)
        if target is None:
            raise ValueError(f"Provider {name!r} is not configured in this session.")
        if target.name == runtime.provider.name:
            # Already active: keep the runtime, selection and grants untouched.
            return False

        group = target.config.model_config[0]
        if confirm(target, group) is not True:
            raise ValueError(f"Provider {name!r} was not approved for this session.")
        credential = resolve_credential(target.config)
        try:
            candidate = build_provider_runtime(
                target, credential, context_budget, group=group.name
            )
        except Exception as exc:
            # Never echo a construction diagnostic that may embed a credential.
            raise ValueError(
                f"Provider {name!r} could not be activated "
                f"({type(exc).__name__}); the current provider is unchanged."
            ) from None
        with commit_guard():
            self.commit(candidate)
        return True
