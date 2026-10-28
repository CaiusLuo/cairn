"""The CLI wires OTLP export beside JSONL only when it is explicitly enabled."""

from pathlib import Path

from cairn.infra.observability.otlp import OtlpSpanExporter
from cairn.infra.observability.sinks import FanoutTraceSink, JsonlTraceSink
from cairn.terminal.cli import _build_trace_sink


def test_export_is_off_without_configuration(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CAIRN_OTEL_EXPORT", raising=False)
    monkeypatch.delenv("CAIRN_OTEL_ENDPOINT", raising=False)
    exporter, sink = _build_trace_sink(tmp_path, None)
    assert exporter is None
    assert isinstance(sink, JsonlTraceSink)
    assert not isinstance(sink, FanoutTraceSink)


def test_export_fans_out_when_enabled(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CAIRN_OTEL_EXPORT", "1")
    monkeypatch.setenv("CAIRN_OTEL_ENDPOINT", "http://127.0.0.1:1/v1/traces")
    exporter, sink = _build_trace_sink(tmp_path, None)
    try:
        assert isinstance(exporter, OtlpSpanExporter)
        assert isinstance(sink, FanoutTraceSink)
        # JSONL remains the first/local sink; the exporter is additive.
        assert isinstance(sink.sinks[0], JsonlTraceSink)
        assert exporter in sink.sinks
    finally:
        if exporter is not None:
            exporter.close(timeout_seconds=1)


def test_env_file_values_can_enable_export(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CAIRN_OTEL_EXPORT", raising=False)
    monkeypatch.delenv("CAIRN_OTEL_ENDPOINT", raising=False)
    exporter, _sink = _build_trace_sink(
        tmp_path,
        {"CAIRN_OTEL_EXPORT": "true", "CAIRN_OTEL_ENDPOINT": "http://127.0.0.1:1/x"},
    )
    try:
        assert isinstance(exporter, OtlpSpanExporter)
    finally:
        if exporter is not None:
            exporter.close(timeout_seconds=1)
