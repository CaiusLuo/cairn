# Tracing, correlation and opt-in OTLP export

Cairn already records one JSONL trace per turn
([`infra.observability`](../../src/cairn/infra/observability)),
finalized by `runtime.turn_trace.TurnTrace` and produced at the
`runtime.loop.run_turn` and `runtime.tool_execution` boundaries. This guide
describes the correlation ids those spans carry, the trigger reasons they record,
and the **default-off** OpenTelemetry/OTLP export.

No framework was added and no second lifecycle exists: the exporter is a
`TraceSink`, the JSONL writer is still the local record, and `run_turn` remains
the only turn owner.

## Correlation ids

A turn admitted by a Session records opaque identifiers on its root
`agent.turn` span:

| Attribute | Source | Present when |
| --- | --- | --- |
| `session_id` | `runtime.session.Session.session_id` | the turn is bound to a Session |
| `turn_id` | the admitted `Turn` | the turn is bound to a Session |
| `task_id` | `DurableSession` task binding | the session is a `DurableSession` with a task |

Ids are attached after admission and only on the root span; every child span
(model attempt, permission, preflight, tool) inherits the same `trace_id` and a
`parent_span_id`. A stateless turn (no Session) records **no** ids rather than
fabricating them. `Session.trace_correlation()` returns only ids — never message
text, paths or credentials.

## Trigger reasons and budgets

Spans already carry budget figures (`max_steps`, `context_max_tokens`,
`context_response_tokens`, token counts and estimate flags). Trigger reasons are
fixed classes, never provider error text:

| Trigger | Attribute(s) |
| --- | --- |
| Compaction | `compaction_count`, `compaction_last_step`, `compaction_last_status`, `compaction_messages_folded`, `compaction_tokens_before/after` |
| Provider fallback | `attempt`, and on attempts after the first `fallback_reason="candidate_unavailable"`, `fallback_attempt` |
| Bounded retry | `exit_code`/`failure_kind` on the tool span; a retried `read_file` appears as a second tool attempt |

Usage that a provider did not report stays **absent** (unknown), never zero.

## Opt-in OTLP export

[`infra.observability.otlp`](../../src/cairn/infra/observability/otlp.py)
forwards a redacted copy of each span to an OTLP/HTTP collector. It is
**disabled by default**; export only happens when explicitly enabled *and* an
endpoint is set.

### Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `CAIRN_OTEL_EXPORT` | `1`/`true`/`yes` enables export | off |
| `CAIRN_OTEL_ENDPOINT` | OTLP/HTTP trailer URL, e.g. `http://127.0.0.1:4318/v1/traces` | unset |
| `CAIRN_OTEL_SERVICE_NAME` | `service.name` resource attribute | `cairn` |
| `CAIRN_OTEL_TIMEOUT_SECONDS` | per-request timeout, `(0, 10]` | `2` |
| `CAIRN_OTEL_QUEUE_SIZE` | bounded queue depth, `[1, 4096]` | `256` |

Both the host environment and `.env` values are read; the host environment wins.
Without `CAIRN_OTEL_EXPORT` enabled and a `CAIRN_OTEL_ENDPOINT`, nothing is
constructed and nothing leaves the machine.

### Compatibility

Any OTLP/HTTP receiver works, including LangSmith's OTLP ingest
(<https://docs.langchain.com/langsmith/trace-with-opentelemetry>): point
`CAIRN_OTEL_ENDPOINT` at the receiver's OTLP/HTTP trace URL and supply its
headers via the receiver's own configuration. Cairn does not create accounts or
upload conversations.

### Redaction

Only allow-listed attributes are exported (`ALLOWED_ATTRIBUTES`): ids, counts,
timings, budget figures, usage numbers, tool names and fixed reason classes.
String values are bounded to 64 characters. Prompts, message content, tool
output, credentials and file paths are **never** exported;
`redact_attributes` drops anything not on the allow list. Lists are exported as
their length, not their members.

### Failure isolation and lifecycle

- `emit` only enqueues; a background worker performs the network call.
- A full queue drops the span (`ExportStats.dropped_full`) instead of stalling.
- Requests have a hard timeout; connection errors and non-2xx responses are
  counted in `ExportStats.failed` and swallowed.
- `close()` stops new spans, flushes within a timeout, and is idempotent.
- The CLI wires the exporter beside the JSONL sink with `FanoutTraceSink`, so a
  remote failure never stops the local record (and vice versa), and closes the
  exporter in bounded terminal cleanup.

JSONL traces remain **best-effort telemetry, not a durable fact** (ADR 0002);
the same is true of exported spans.

## Verification

- [`tests/infra/observability/test_otlp_export.py`](../../tests/infra/observability/test_otlp_export.py)
  uses a local fake OTLP collector (a real HTTP server on `127.0.0.1`) to check
  correlation ids, redaction, failure isolation, queue overflow, fan-out and
  cleanup. No SaaS account, no external network, no real conversation.
- [`tests/runtime/test_turn_correlation.py`](../../tests/runtime/test_turn_correlation.py)
  checks the ids and trigger reasons reach the spans through the real `run_turn`.

## Out of scope this round

Host probes, standing alerts and a management dashboard are **not** built here.
They remain future boundaries: this guide covers correlation, trigger reasons and
optional export only.
