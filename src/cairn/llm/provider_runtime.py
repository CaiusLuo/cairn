"""One provider's fully constructed runtime, bound to its endpoint and credential.

Every provider owns its model manager, executor, runtime factory, selected
client and context builder. A switch builds the target's ``ProviderRuntime``
completely before anything in the active session is rebound, so denied
approval, a missing credential or a construction failure leaves the active
provider untouched.
"""

from collections.abc import Callable
from dataclasses import dataclass

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.llm.base import LLMClient
from cairn.llm.model_executor import ModelExecutor
from cairn.llm.model_manager import ModelManager
from cairn.llm.provider_catalog import NamedProvider

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
    from cairn.llm.litellm_client import LiteLLMClient
    from cairn.llm.token_counter import LiteLLMTokenCounter

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
