from collections.abc import Callable
from dataclasses import dataclass

from cairn.llm.model_executor import ModelExecutor
from cairn.llm.model_manager import ModelConfig, ModelManager
from cairn.observability.storage import TraceStore


@dataclass
class CommandContext:
    trace_store: TraceStore
    last_trace_id: str | None = None
    model_manager: ModelManager | None = None
    model_executor: ModelExecutor | None = None
    select_model: Callable[[str], ModelConfig] | None = None
