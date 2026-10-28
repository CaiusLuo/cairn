"""Opt-in OpenTelemetry/OTLP export of already-redacted trace spans.

This is a *sink*, not a tracing system: ``Tracer`` still owns span lifecycle and
``JsonlTraceSink`` stays the local best-effort record. This exporter forwards a
sanitized copy of each span to an OTLP/HTTP collector and is **disabled by
default**. It is compatible with any OTLP receiver, including LangSmith's OTLP
ingest (see https://docs.langchain.com/langsmith/trace-with-opentelemetry).

Guarantees this module is responsible for:

* **Opt-in.** ``resolve_export_config`` returns ``None`` unless explicitly
  enabled *and* an endpoint is configured. No configuration means no export.
* **Never breaks the task.** ``emit`` enqueues and returns; a background worker
  performs the network call. Every failure (queue full, timeout, connection
  error, non-2xx) is swallowed and counted, never raised into the caller.
* **Bounded.** A fixed-size queue drops the oldest-pending span under pressure,
  requests have a hard timeout, and ``close`` flushes for at most the timeout.
* **Redacted.** Only allow-listed attributes are exported. Prompts, tool output,
  message content, credentials and absolute/relative file paths are never sent;
  unknown usage stays absent (``null``) rather than zero.

The local JSONL trace remains telemetry, not a durable fact (ADR 0002).
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.request import Request, urlopen

from cairn.infra.observability.models import Span, SpanStatus

#: Attributes safe to export. Everything else is dropped. These are ids, counts,
#: timings, budget figures and fixed reason classes — never content.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # correlation
        "session_id",
        "turn_id",
        "task_id",
        "provider",
        # budgets / context
        "max_steps",
        "context_max_tokens",
        "context_response_tokens",
        "context_tokens_before",
        "context_tokens_after",
        "context_count_is_estimate",
        "context_compacted",
        "context_compacted_messages",
        "context_compaction_tokens_saved",
        "context_folded_tool_results",
        "context_omitted_turns",
        "context_omitted_messages",
        "message_count",
        "tool_schema_count",
        "step",
        # compaction trigger
        "compaction_count",
        "compaction_last_step",
        "compaction_last_status",
        "compaction_messages_folded",
        "compaction_tokens_before",
        "compaction_tokens_after",
        # fallback / retry trigger
        "attempt",
        "fallback_reason",
        "fallback_attempt",
        # model identity and usage
        "model",
        "model_name",
        "tool_call_count",
        "has_content",
        "input_tokens",
        "output_tokens",
        # tools / permissions
        "tool",
        "exit_code",
        "cancelled",
        "error_type",
        "failure_kind",
        # memory (ids/counts only)
        "memory_tokens",
        "memory_checked_count",
        "memory_read_error",
    }
)

#: Attribute values are short scalars. A path-like or long string never survives.
MAX_ATTRIBUTE_STRING = 64

#: Canonical tool names from the trusted registry (``build_default_registry`` and
#: the optional ``repo_graph`` tool). A span's ``tool`` attribute is the
#: model-provided call name; only an exact match here is exported. Anything else —
#: a short sensitive string, a path, or an illegal name — becomes the fixed
#: ``unknown_tool`` category, so a model cannot smuggle text through a tool name.
SAFE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "bash",
        "read_file",
        "edit_file",
        "write_file",
        "patch_file",
        "glob",
        "grep",
        "read_skill",
        "repo_graph",
    }
)
UNKNOWN_TOOL = "unknown_tool"


def _safe_value(key: str, value: Any) -> Any | None:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_ATTRIBUTE_STRING:
            return None
        return value
    # Lists of ids/counts are exported as counted lists, never raw entries.
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def sanitize_tool_name(value: Any) -> str:
    """Map a tool identity to a trusted canonical name or a fixed category.

    Only an exact allow-listed registry name passes through; everything else
    (unknown, path-like, over-long or non-string) collapses to ``unknown_tool``.
    """
    if isinstance(value, str) and value in SAFE_TOOL_NAMES:
        return value
    return UNKNOWN_TOOL


def redact_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Return only allow-listed, scalar, bounded, sanitized attributes."""
    redacted: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES:
            continue
        if key == "tool":
            # Tool identity never passes through raw: only a trusted name.
            redacted[key] = sanitize_tool_name(value)
            continue
        safe = _safe_value(key, value)
        if safe is not None:
            redacted[key] = safe
    return redacted


@dataclass(frozen=True, slots=True)
class ExportConfig:
    """Resolved opt-in configuration; construction proves enablement."""

    endpoint: str
    service_name: str = "cairn"
    timeout_seconds: float = 2.0
    queue_size: int = 256

    def __post_init__(self) -> None:
        if not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("OTLP endpoint must be an http(s) URL")
        if not 0 < self.timeout_seconds <= 10:
            raise ValueError("OTLP timeout must be in (0, 10]")
        if not 1 <= self.queue_size <= 4096:
            raise ValueError("OTLP queue size must be in [1, 4096]")


def resolve_export_config(
    environ: Mapping[str, str],
) -> ExportConfig | None:
    """Return a config only when explicitly enabled with an endpoint.

    ``CAIRN_OTEL_EXPORT`` must be ``1``/``true``/``yes`` and
    ``CAIRN_OTEL_ENDPOINT`` must be set. Anything else returns ``None`` (off).
    """
    enabled = environ.get("CAIRN_OTEL_EXPORT", "").strip().lower()
    if enabled not in {"1", "true", "yes"}:
        return None
    endpoint = environ.get("CAIRN_OTEL_ENDPOINT", "").strip()
    if not endpoint:
        return None
    timeout_raw = environ.get("CAIRN_OTEL_TIMEOUT_SECONDS", "").strip()
    queue_raw = environ.get("CAIRN_OTEL_QUEUE_SIZE", "").strip()
    return ExportConfig(
        endpoint=endpoint,
        service_name=environ.get("CAIRN_OTEL_SERVICE_NAME", "cairn").strip() or "cairn",
        timeout_seconds=float(timeout_raw) if timeout_raw else 2.0,
        queue_size=int(queue_raw) if queue_raw else 256,
    )


def _to_nanos(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(aware.timestamp() * 1_000_000_000)


def _status_code(status: SpanStatus) -> int:
    return {SpanStatus.UNSET: 0, SpanStatus.OK: 1, SpanStatus.ERROR: 2}[status]


def span_to_otlp_json(span: Span, service_name: str) -> dict[str, Any]:
    """Render one span as an OTLP/HTTP JSON resourceSpans value.

    Only redacted attributes, ids, names, timing and status are present.
    """
    attributes = [
        {"key": key, "value": {"stringValue": str(value)}}
        if isinstance(value, str)
        else {"key": key, "value": {"boolValue": value}}
        if isinstance(value, bool)
        else {"key": key, "value": {"intValue": str(int(value))}}
        for key, value in redact_attributes(span.attributes).items()
    ]
    end_time = span.end_time
    span_json: dict[str, Any] = {
        "traceId": span.context.trace_id,
        "spanId": span.context.span_id,
        "name": span.name,
        "kind": 1,
        "startTimeUnixNano": str(_to_nanos(span.start_time)),
        "attributes": attributes,
        "status": {"code": _status_code(span.status)},
    }
    if span.context.parent_span_id is not None:
        span_json["parentSpanId"] = span.context.parent_span_id
    if end_time is not None:
        span_json["endTimeUnixNano"] = str(_to_nanos(end_time))
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}}
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "cairn.trace"}, "spans": [span_json]}
                ],
            }
        ]
    }


@dataclass(slots=True)
class ExportStats:
    """Observed outcomes; the exporter is best-effort and reports what happened."""

    enqueued: int = 0
    dropped_full: int = 0
    sent: int = 0
    failed: int = 0
    last_error: str | None = field(default=None)


class OtlpSpanExporter:
    """A ``TraceSink`` that forwards redacted spans to an OTLP/HTTP endpoint."""

    def __init__(self, config: ExportConfig) -> None:
        self.config = config
        self.stats = ExportStats()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=config.queue_size
        )
        self._closed = False
        #: Set by ``close``; the worker drains the queue then exits, so a full
        #: queue can never strand the worker waiting for a sentinel that was
        #: dropped.
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="cairn-otlp-export", daemon=True
        )
        self._worker.start()

    def emit(self, span: Span) -> None:
        """Enqueue a sanitized span; never raises and never blocks the task."""
        if self._closed:
            return
        payload = span_to_otlp_json(span, self.config.service_name)
        try:
            self._queue.put_nowait(payload)
            self.stats.enqueued += 1
        except queue.Full:
            # Best-effort telemetry: drop rather than stall the turn.
            self.stats.dropped_full += 1

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                # Exit only once stopped *and* the queue is drained.
                if self._stop.is_set():
                    return
                continue
            try:
                self._post(item)
            finally:
                self._queue.task_done()

    def _post(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                response.read()
                if not 200 <= response.status < 300:
                    raise OSError(f"OTLP collector returned {response.status}")
        except Exception as exc:
            self.stats.failed += 1
            self.stats.last_error = f"{type(exc).__name__}"
            return
        self.stats.sent += 1

    def close(self, timeout_seconds: float | None = None) -> None:
        """Stop accepting spans, drain the queue, and boundedly join the worker.

        Idempotent and safe to repeat: after setting the stop flag the worker
        drains whatever is queued and then exits, so a full queue cannot leave it
        blocked. A repeated call re-joins in case an earlier join timed out.
        """
        self._closed = True
        self._stop.set()
        self._worker.join(timeout=timeout_seconds or self.config.timeout_seconds)


def build_exporter(environ: Mapping[str, str]) -> OtlpSpanExporter | None:
    """Construct an exporter from the environment, or ``None`` when off."""
    config = resolve_export_config(environ)
    return OtlpSpanExporter(config) if config is not None else None
