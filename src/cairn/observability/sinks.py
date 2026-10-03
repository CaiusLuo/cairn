from pathlib import Path
from typing import Protocol

from cairn.observability.models import Span


class TraceSink(Protocol):
    def emit(self, span: Span) -> None: ...


class JsonlTraceSink:
    """Append-only JSONL writer: the sole persistent record of a trace."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def emit(self, span: Span) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        path = self.root / f"{span.context.trace_id}.jsonl"

        with path.open("a", encoding="utf-8") as f:
            f.write(span.model_dump_json())
            f.write("\n")
