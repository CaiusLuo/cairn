from pathlib import Path

class TraceResolver:
    def __init__(self, root: Path):
        self.root = root

    def resolve(self, trace_id: str) -> Path:
        if not trace_id:
            raise ValueError("Trace ID is empty")

        files = [
            path.stem
            for path in self.root.glob("*.json")
            if path.stem.startswith(trace_id)
        ]

        if not files:
            raise FileNotFoundError(f"Trace not found: {trace_id}")

        if len(files) > 1:
            raise ValueError(f"Ambiguous trace prefix: {trace_id}")

        return files[0]
