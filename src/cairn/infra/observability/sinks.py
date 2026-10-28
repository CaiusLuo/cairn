from pathlib import Path
from typing import Protocol

from cairn.infra.observability.models import Span


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


class FanoutTraceSink:
    """Emit each span to several sinks, isolating their failures.

    The local JSONL sink and an optional remote exporter are independent: a
    remote export failure must not stop the local record, and a local write
    failure must not stop the export. The first failure is re-raised only after
    every sink has been given the span, so ``Tracer`` still observes it.
    """

    def __init__(self, *sinks: TraceSink) -> None:
        self.sinks = sinks

    def emit(self, span: Span) -> None:
        first_error: Exception | None = None
        for sink in self.sinks:
            try:
                sink.emit(span)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def close(self, timeout_seconds: float | None = None) -> None:
        for sink in self.sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                try:
                    close(timeout_seconds)
                except Exception:
                    continue
