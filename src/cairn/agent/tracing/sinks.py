from pathlib import Path
from typing import Protocol

from cairn.agent.tracing.models import Span
from cairn.shared.redaction import redact_text as _redact_text
from cairn.shared.redaction import redact_value as _redact_value


class TraceSink(Protocol):
    def emit(self, span: Span) -> None: ...


class JsonlTraceSink:
    """Append-only JSONL writer: the sole persistent record of a trace."""

    def __init__(self, root: Path, *, max_file_bytes: int | None = None) -> None:
        self.root = root
        self.max_file_bytes = max_file_bytes

    def emit(self, span: Span) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        path = self.root / f"{span.context.trace_id}.jsonl"

        line = (span.model_dump_json() + "\n").encode("utf-8")
        with path.open("ab") as f:
            if (
                self.max_file_bytes is not None
                and f.tell() + len(line) > self.max_file_bytes
            ):
                raise OSError("trace file limit reached")
            f.write(line)


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


class RedactingTraceSink:
    """Wrap a sink so declared credential values never reach the persisted trace.

    Span ``error`` text and string attributes can carry provider/tool exception
    messages. A configured credential that appears there is replaced with
    ``[redacted]`` before the inner sink writes it, so a saved JSONL trace cannot
    leak a secret even when the model or provider echoes it. Non-string attribute
    values are untouched.
    """

    def __init__(
        self,
        inner: TraceSink,
        secret_values: tuple[str, ...],
        *,
        metadata_only: bool = False,
    ) -> None:
        self.inner = inner
        self.metadata_only = metadata_only
        self.secret_values = tuple(value for value in secret_values if value)

    def emit(self, span: Span) -> None:
        if not self.secret_values and not self.metadata_only:
            self.inner.emit(span)
            return
        redacted = span.model_copy(deep=True)
        if redacted.error is not None:
            redacted.error = (
                "details_omitted"
                if self.metadata_only
                else _redact_text(redacted.error, self.secret_values)
            )
        redacted.attributes = _redact_value(redacted.attributes, self.secret_values)
        self.inner.emit(redacted)
