from pathlib import Path
from typing import Any, Protocol

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


def _redact_text(text: str, secret_values: "tuple[str, ...]") -> str:
    clean = text
    for value in secret_values:
        if value:
            clean = clean.replace(value, "[redacted]")
    return clean


def _redact_value(value: object, secret_values: "tuple[str, ...]") -> Any:
    if isinstance(value, str):
        return _redact_text(value, secret_values)
    if isinstance(value, list):
        return [_redact_value(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secret_values) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item, secret_values) for key, item in value.items()}
    return value


class RedactingTraceSink:
    """Wrap a sink so declared credential values never reach the persisted trace.

    Span ``error`` text and string attributes can carry provider/tool exception
    messages. A configured credential that appears there is replaced with
    ``[redacted]`` before the inner sink writes it, so a saved JSONL trace cannot
    leak a secret even when the model or provider echoes it. Non-string attribute
    values are untouched.
    """

    def __init__(self, inner: TraceSink, secret_values: "tuple[str, ...]") -> None:
        self.inner = inner
        self.secret_values = tuple(value for value in secret_values if value)

    def emit(self, span: Span) -> None:
        if not self.secret_values:
            self.inner.emit(span)
            return
        redacted = span.model_copy(deep=True)
        if redacted.error is not None:
            redacted.error = _redact_text(redacted.error, self.secret_values)
        redacted.attributes = _redact_value(redacted.attributes, self.secret_values)
        self.inner.emit(redacted)
