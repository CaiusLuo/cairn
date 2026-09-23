from pathlib import Path


class TraceResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, trace_id: str) -> str:
        if not trace_id or Path(trace_id).name != trace_id:
            raise ValueError(f"Invalid trace ID: {trace_id}")

        files = [
            path.stem
            for path in self.root.glob("*.jsonl")
            if path.stem.startswith(trace_id)
        ]

        if not files:
            raise FileNotFoundError(f"Trace not found: {trace_id}")

        if len(files) > 1:
            raise ValueError(f"Ambiguous trace prefix: {trace_id}")

        return files[0]
