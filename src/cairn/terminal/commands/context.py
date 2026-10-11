from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cairn.core.model_manager import ModelConfig, ModelManager
from cairn.core.provider_catalog import ProviderCatalog
from cairn.infra.observability.storage import TraceStore
from cairn.runtime.model_executor import ModelExecutor


@dataclass
class CommandContext:
    trace_store: TraceStore
    last_trace_id: str | None = None
    model_manager: ModelManager | None = None
    model_executor: ModelExecutor | None = None
    select_model: Callable[[str], ModelConfig] | None = None
    choose_model: (
        Callable[[str, tuple[ModelConfig, ...], str], Awaitable[str | None]] | None
    ) = None
    provider_catalog: ProviderCatalog | None = None
    active_provider_name: str | None = None
    select_provider: Callable[[str], None] | None = None
