from pathlib import Path

from cairn.observability.models import Span, SpanStatus, TraceListResult, TraceSummary
from cairn.observability.resolver import TraceResolver
from cairn.observability.sinks import write_summary


class JsonlTraceReader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.resolver = TraceResolver(root)

    def _read_path(self, stem: str) -> list[Span]:
        path = self.root / f"{stem}.jsonl"

        if not path.exists():
            raise FileNotFoundError(f"Trace not found: {stem}")

        spans: list[Span] = []

        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            spans.append(Span.model_validate_json(line))

        return spans

    def read(self, trace_id: str) -> list[Span]:
        resolved_id = self.resolver.resolve(trace_id)

        return self._read_path(resolved_id)

    def _read_summary(self, stem: str) -> TraceSummary:
        path = self.root / "summaries" / f"{stem}.json"
        summary = TraceSummary.model_validate_json(path.read_text(encoding="utf-8"))
        if summary.trace_id != stem:
            raise ValueError("Summary trace ID does not match its filename")
        return summary

    def list_traces(self, limit: int = 20) -> TraceListResult:
        result = TraceListResult()

        for path in self.root.glob("*.jsonl"):
            short_id = path.stem[:8]
            summary_error: str | None = None
            try:
                summary = self._read_summary(path.stem)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                summary_error = type(exc).__name__
            else:
                result.traces.append(summary)
                continue

            try:
                spans = self._read_path(path.stem)
                root = next(
                    (
                        span
                        for span in spans
                        if span.context.parent_span_id is None
                        and span.end_time is not None
                        and span.status != SpanStatus.UNSET
                    ),
                    None,
                )
                if root is None:
                    result.diagnostics.append(f"skipped incomplete trace {short_id}")
                    continue
                summary = TraceSummary.from_root(root)
                if summary.trace_id != path.stem:
                    raise ValueError("Root trace ID does not match its filename")
            except (OSError, ValueError) as exc:
                result.diagnostics.append(
                    f"skipped corrupt trace {short_id} ({type(exc).__name__})"
                )
                continue

            try:
                write_summary(self.root, summary)
            except (OSError, ValueError) as exc:
                result.diagnostics.append(
                    f"could not save summary for trace {short_id} "
                    f"({type(exc).__name__})"
                )
            else:
                if summary_error is not None:
                    result.diagnostics.append(
                        f"recovered summary for trace {short_id} ({summary_error})"
                    )
            result.traces.append(summary)

        result.traces.sort(
            key=lambda summary: summary.start_time.timestamp(), reverse=True
        )
        result.traces = result.traces[:limit]
        return result
