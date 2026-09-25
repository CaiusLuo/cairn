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

    def list_traces(self, limit: int = 20) -> list[Span]:
        roots: list[Span] = []

        for path in self.root.glob("*.jsonl"):
            spans = self.read(path.stem)

            root = next(
                (span for span in spans if span.context.parent_span_id is None),
                None,
            )

            if root is not None:
                roots.append(root)

        roots.sort(key=lambda span: span.start_time, reverse=True)

        return roots[:limit]
