from dataclasses import dataclass

from cairn.observability.reader import JsonlTraceReader


@dataclass
class CommandContext:
    trace_reader: JsonlTraceReader
    last_trace_id: str | None = None
