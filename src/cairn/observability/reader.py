from pathlib import Path

from cairn.observability.models import Span


class JsonlTraceReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, trace_id: str) -> list[Span]:
        if not trace_id or Path(trace_id).name != trace_id:
            raise ValueError(f"Invalid trace ID: {trace_id}")

        path = self.root / f"{trace_id}.jsonl"

        if not path.exists():
            raise FileNotFoundError(f"Trace not found: {trace_id}")

        spans: list[Span] = []

        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            spans.append(Span.model_validate_json(line))

        return spans
