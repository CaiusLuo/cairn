# Coding Task summaries

`CodingTaskRunner.last_summary` is a detached, immutable observation of a settled
`run()` call. It leaves `TaskResult` unchanged. A completed runtime does not imply
correct output: `verification_verdict` stays `null` until the host supplies real
deterministic check results.

## Retrieve and export

Use the existing runner, trace sink and checks. For example, after a Task has
returned in its caller-owned Workspace:

```python
from pathlib import Path

from cairn.infra.observability.storage import TraceStore
from cairn.runtime.evals import FileContentEqualsCheck
from cairn.runtime.tasks import summarize_task

result = await runner.run(spec)
base = runner.last_summary
if base is not None:
    # expected_bytes_as_text is a host-defined oracle, never a model claim.
    check = FileContentEqualsCheck("src/range_utils.py", expected_bytes_as_text)
    checks = [await check.evaluate(runner.workspace)]
    summary = summarize_task(
        base, trace_store=TraceStore(trace_directory), checks=checks
    )
    Path("task-summary.json").write_text(summary.to_json(), encoding="utf-8")
```

Omit `checks` when no verification ran; omit `trace_store` when no trace is
available. Export is an explicit host operation after execution, with its own I/O
error handling. `to_json()` validates the scalar-only schema and limits the UTF-8
artifact to 4 KiB. No file is written automatically and no second database is
created. A summary can be loaded with `TaskExecutionSummary.model_validate_json()`.

After external cancellation, `run()` still raises the original `CancelledError`
after owned work settles. `last_summary`, when available, records
`cancellation_propagated=true`. Without a produced result it records `cancelled`
and `result_available=false`; it does not manufacture a `TaskResult`. A late
delivery cancellation preserves an already produced result's actual status and
sets `result_available=true`. Cooperative
cancellation returns the existing cancelled result. Observation failures can
leave `last_summary=None`; neither failure changes execution, cleanup, receipts,
writer ownership or recovery. Read the snapshot immediately after the relevant
call and retain it for later use. Concurrent calls share the last-settled slot;
use separate runners when each caller needs its own retained slot.

## Sources of truth and unavailable values

| Summary value | Owner and meaning |
| --- | --- |
| Task ID, status, result availability | Runner's existing TaskSpec/TaskResult or propagated cancellation |
| Session/Turn IDs | Existing SessionEvent attribution; stateless runs leave both null |
| Trace ID and persistence | Existing trace_start/trace_finish; no invented ID when tracing is absent |
| Elapsed seconds | Runner's monotonic clock, including repository inspection and cancellation settlement; null if unavailable |
| Model attempts | Count of existing llm.generate spans, including fallback failures; these are runtime attempts, not observed HTTP request counts |
| Model failures | Existing noncancelled failed llm.generate spans; an observed attempt failure need not be the terminal Task failure |
| Tool attempts | Existing permission.check plus failed tool.preflight spans, including denied or invalid calls |
| Tool executions/failures | Existing tool.execute spans and failed preflights; cancellations are not counted as tool failures |
| Permission denials | Existing permission.check spans with allowed=false |
| Input/output tokens | TurnTrace's final trace_finish totals, copied independently; no summing child spans or context estimates |
| Verification | Only caller-provided CheckResult values, reduced to count and pass/fail/error; absent or empty checks remain null |
| Cost | Always null: this API has no trustworthy attributable pricing source |

Call counts require a readable completed root and consistent span identities.
Without that evidence they stay null, rather than zero. A complete trace with no
tool calls can establish an actual zero. `trace_diagnostic` distinguishes an
unrequested lookup, sink failure, unavailable trace and incomplete trace. A sink
failure preserves any usage reported by `trace_finish`, but cannot establish
complete call counts. No tracer means that final usage event is unavailable even
if a client internally knew tokens; this API does not create a second usage owner.

`failure_category` uses a fixed vocabulary: `cancelled`, `budget_exhausted`,
`inspection_failed`, `timeout`, or `runtime_error`. TaskResult has no typed
causal link to child spans, so a generic runtime error cannot reliably be
attributed to a preceding model/tool/permission failure. Observed failures are
reported separately: the last one may have recovered before an unrelated host
callback fails. A recovered tool failure or denial does not turn a completed
Task into a failed Task; its counters still describe the failed operation.
`timeout_observed` is true only for a caught TimeoutError or a typed tool timeout
span. It otherwise remains null. Bash's untyped nonzero result and an external
deadline's cancellation do not establish timeout attribution.

## Export boundaries

The summary copies no prompt, model answer, tool arguments/output, exception
text, repository paths, check text or arbitrary trace metadata. Task IDs must be
opaque host labels. IDs outside the bounded label syntax, or containing a known
host credential value, are replaced with a SHA-256 alias (`task_id_hashed=true`).
Credential names come from Cairn's existing filter and the runner's
`secret_env_keys`; this code never resolves provider credentials or loads `.env`.
Session/Turn/Trace identities accept only UUIDs. Export the summary rather than
serializing TaskResult or raw events when only diagnostics are needed.

This is a snapshot, not a verification receipt or publication authorization.
Results are not persisted or reconstructed automatically after process death.
EvalRunner retains its own existing EvalMetrics and verification ownership.
The API does not add metrics display to the experimental Pi TUI's per-turn UI.
