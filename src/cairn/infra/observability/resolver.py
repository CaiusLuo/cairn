from bisect import insort
from pathlib import Path


class TraceResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, trace_id: str) -> str:
        if not trace_id or Path(trace_id).name != trace_id:
            raise ValueError(f"Invalid trace ID: {trace_id}")

        match_count = 0
        matched_stem: str | None = None
        preview_stems: list[str] = []
        for path in self.root.glob("*.jsonl"):
            stem = path.stem
            if not stem.startswith(trace_id):
                continue
            match_count += 1
            matched_stem = stem
            insort(preview_stems, stem[:8])
            if len(preview_stems) > 5:
                preview_stems.pop()

        if match_count == 0:
            raise FileNotFoundError(f"Trace not found: {trace_id}")

        if match_count > 1:
            preview = ", ".join(preview_stems)
            more = f", +{match_count - 5} more" if match_count > 5 else ""
            raise ValueError(
                f"Ambiguous trace prefix: {trace_id} "
                f"({match_count} matches: {preview}{more})"
            )

        assert matched_stem is not None
        return matched_stem
