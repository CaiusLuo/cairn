from pathlib import Path

from cairn.observability.models import Span
from cairn.observability.resolver import TraceResolver


class JsonlTraceReader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.resolver = TraceResolver(root)

    def read(self, trace_id: str) -> list[Span]:
        resolved_id = self.resolver.resolve(trace_id)

        path = self.root / f"{resolved_id}.jsonl"

        if not path.exists():
            raise FileNotFoundError(f"Trace not found: {trace_id}")

        spans: list[Span] = []

        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            spans.append(Span.model_validate_json(line))

        return spans
