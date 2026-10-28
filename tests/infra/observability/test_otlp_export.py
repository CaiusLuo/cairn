"""Opt-in OTLP export: correlation, redaction, failure isolation and cleanup.

A local fake collector (a real HTTP server on localhost) stands in for an OTLP
receiver. No SaaS account, no real conversation and no external network leave the
machine.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cairn.infra.observability.models import SpanStatus, new_trace_context, start_span
from cairn.infra.observability.otlp import (
    ALLOWED_ATTRIBUTES,
    ExportConfig,
    OtlpSpanExporter,
    build_exporter,
    redact_attributes,
    resolve_export_config,
)
from cairn.infra.observability.sinks import FanoutTraceSink, JsonlTraceSink


class _Collector:
    """A local OTLP/HTTP JSON receiver that records requests."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self.headers: list[dict[str, str]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                collector.requests.append(json.loads(body.decode()))
                collector.headers.append(dict(self.headers))
                self.send_response(collector.status)
                self.end_headers()
                self.wfile.write(b"")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        address = self.server.server_address
        host, port = str(address[0]), int(address[1])
        return f"http://{host}:{port}/v1/traces"

    def __enter__(self) -> "_Collector":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _span(name: str = "agent.turn", **attributes: Any):  # type: ignore[no-untyped-def]
    context = new_trace_context()
    span = start_span(name, context, attributes=dict(attributes))
    span.end_time = span.start_time
    span.status = SpanStatus.OK
    return span


@pytest.fixture
def collector() -> Iterator[_Collector]:
    with _Collector() as server:
        yield server


def test_export_is_off_by_default() -> None:
    assert resolve_export_config({}) is None
    assert resolve_export_config({"CAIRN_OTEL_EXPORT": "1"}) is None  # no endpoint
    assert resolve_export_config({"CAIRN_OTEL_ENDPOINT": "http://x"}) is None  # off
    assert build_exporter({}) is None


def test_config_requires_explicit_enable_and_valid_endpoint() -> None:
    config = resolve_export_config(
        {"CAIRN_OTEL_EXPORT": "true", "CAIRN_OTEL_ENDPOINT": "http://127.0.0.1:4318"}
    )
    assert config is not None and config.endpoint.startswith("http://")
    with pytest.raises(ValueError, match="http"):
        ExportConfig(endpoint="ftp://collector")
    with pytest.raises(ValueError, match="timeout"):
        ExportConfig(endpoint="http://x", timeout_seconds=0)


def test_redaction_drops_prompts_paths_and_credentials() -> None:
    attributes = {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "task_id": "task-1",
        "step": 2,
        "input_tokens": 10,
        # Everything below must not survive:
        "prompt": "secret prompt text",
        "tool_output": "file contents with /etc/passwd",
        "path": "/Users/caius/private/file.py",
        "api_key": "sk-live-123",
        "message_count": 4,
        "unknown_field": "value",
        "long_id": "x" * 500,
    }
    redacted = redact_attributes(attributes)
    assert redacted["session_id"] == "sess-1"
    assert redacted["step"] == 2
    assert redacted["input_tokens"] == 10
    for forbidden in ("prompt", "tool_output", "path", "api_key", "unknown_field"):
        assert forbidden not in redacted
    assert "long_id" not in redacted
    dumped = json.dumps(redacted)
    assert "sk-live" not in dumped and "/etc/passwd" not in dumped
    assert set(redacted) <= ALLOWED_ATTRIBUTES


def test_correlation_ids_reach_the_collector(collector: _Collector) -> None:
    exporter = OtlpSpanExporter(ExportConfig(endpoint=collector.endpoint))
    span = _span(
        "agent.turn",
        session_id="sess-xyz",
        turn_id="turn-abc",
        task_id="task-123",
        step=1,
    )
    exporter.emit(span)
    exporter.close(timeout_seconds=5)
    assert collector.requests, "collector received no span"
    payload = collector.requests[0]
    resource_spans = payload["resourceSpans"][0]
    assert (
        resource_spans["resource"]["attributes"][0]["value"]["stringValue"] == "cairn"
    )
    span_json = resource_spans["scopeSpans"][0]["spans"][0]
    assert span_json["traceId"] == span.context.trace_id
    keys = {item["key"]: item["value"] for item in span_json["attributes"]}
    assert keys["session_id"]["stringValue"] == "sess-xyz"
    assert keys["turn_id"]["stringValue"] == "turn-abc"
    assert keys["task_id"]["stringValue"] == "task-123"


def test_failed_export_never_raises_and_is_counted() -> None:
    with _Collector(status=500) as bad:
        exporter = OtlpSpanExporter(ExportConfig(endpoint=bad.endpoint))
        exporter.emit(_span("agent.turn", step=1))  # must not raise
        exporter.close(timeout_seconds=5)
        assert exporter.stats.failed >= 1
        assert exporter.stats.sent == 0


def test_unreachable_collector_never_raises() -> None:
    # A port nothing listens on: the request fails, the task is unaffected.
    exporter = OtlpSpanExporter(
        ExportConfig(endpoint="http://127.0.0.1:1/v1/traces", timeout_seconds=0.2)
    )
    exporter.emit(_span("agent.turn"))
    exporter.close(timeout_seconds=1)
    assert exporter.stats.failed >= 1


def test_queue_full_drops_instead_of_blocking() -> None:
    config = ExportConfig(endpoint="http://127.0.0.1:1/v1/traces", queue_size=1)
    exporter = OtlpSpanExporter(config)
    # Emit more than the queue can hold while the worker is stuck failing.
    for _ in range(50):
        exporter.emit(_span("agent.turn"))
    assert exporter.stats.dropped_full >= 0  # never raises; drops are counted
    exporter.close(timeout_seconds=1)


def test_fanout_isolates_sink_failures(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class BrokenSink:
        def __init__(self) -> None:
            self.count = 0

        def emit(self, span) -> None:  # type: ignore[no-untyped-def]
            self.count += 1
            raise OSError("boom")

    broken = BrokenSink()
    jsonl = JsonlTraceSink(tmp_path)
    fanout = FanoutTraceSink(broken, jsonl)
    span = _span("agent.turn", step=1)
    with pytest.raises(OSError):
        fanout.emit(span)
    # The healthy sink still recorded the span.
    assert broken.count == 1
    assert (tmp_path / f"{span.context.trace_id}.jsonl").is_file()


def test_fanout_close_cleans_up_collector(collector: _Collector) -> None:
    exporter = OtlpSpanExporter(ExportConfig(endpoint=collector.endpoint))
    fanout = FanoutTraceSink(exporter)
    fanout.emit(_span("agent.turn", session_id="s1"))
    fanout.close(timeout_seconds=5)
    assert exporter._closed is True
    assert exporter._worker.is_alive() is False
    assert collector.requests


def test_payload_contains_no_prompt_or_path(collector: _Collector) -> None:
    exporter = OtlpSpanExporter(ExportConfig(endpoint=collector.endpoint))
    span = _span(
        "llm.generate",
        step=1,
        prompt="please read /etc/shadow",
        path="/home/user/.ssh/id_rsa",
        provider="test",
    )
    exporter.emit(span)
    exporter.close(timeout_seconds=5)
    raw = json.dumps(collector.requests)
    assert "/etc/shadow" not in raw and "id_rsa" not in raw
