import os
import tempfile
from pathlib import Path
from typing import Protocol

from cairn.observability.models import Span, SpanStatus, TraceSummary


class TraceSink(Protocol):
    def emit(self, span: Span) -> None: ...


def write_summary(root: Path, summary: TraceSummary) -> None:
    summary_root = root / "summaries"
    summary_root.mkdir(parents=True, exist_ok=True)
    path = summary_root / f"{summary.trace_id}.json"
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=summary_root,
            prefix=f".{summary.trace_id}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(summary.model_dump_json())
            temporary_file.flush()

        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class JsonlTraceSink:
    def __init__(self, root: Path) -> None:
        self.root = root

    def emit(self, span: Span) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        path = self.root / f"{span.context.trace_id}.jsonl"

        with path.open("a", encoding="utf-8") as f:
            f.write(span.model_dump_json())
            f.write("\n")

        if (
            span.context.parent_span_id is None
            and span.end_time is not None
            and span.status in (SpanStatus.OK, SpanStatus.ERROR)
        ):
            write_summary(self.root, TraceSummary.from_root(span))
