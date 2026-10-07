from dataclasses import dataclass

from cairn.observability.storage import TraceStore


@dataclass
class CommandContext:
    trace_store: TraceStore
    last_trace_id: str | None = None
