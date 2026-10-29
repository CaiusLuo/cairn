# Server single-task entry (`cairn task run`)

A Linux-server, single-shot, non-interactive entry that runs exactly one coding
task against an **explicit Workspace** through the existing `CodingTaskRunner` /
`runtime.loop.run_turn` boundary, then returns a machine-readable result. It does
not add a second Agent loop, a queue, goals or scheduling.

```sh
cairn task run \
  --workspace /srv/project \
  --prompt "Fix the failing parser test" \
  --trust-workspace \
  --approve-provider-name default \
  --approve-provider-endpoint https://api.example.com/v1 \
  --approve-provider-group primary \
  --approve-provider-key-env CAIRN_API_KEY \
  --approve-provider-models "openai/model-a,openai/model-b" \
  [--allow-shell] [--json] [--report result.json]
```

`--prompt-file PATH` may replace `--prompt`. `--max-steps` bounds the turn.
`--trace-dir` defaults to `<workspace>/.cairn/traces`.

## Authorization (operator, never the prompt)

Authority comes only from the operator's CLI facts, never from the task text, a
model argument or a tool name. There is no interactive prompt: a capability or
provider the operator did not approve is refused immediately rather than waiting
for input.

| Fact | Binds |
| --- | --- |
| `--trust-workspace` | read/write inside the Workspace; without it the launch is `unauthorized` with no effect |
| `--approve-provider-name` | provider identity (`default` for the `.env` layout) |
| `--approve-provider-endpoint` | provider base URL |
| `--approve-provider-group` | selected model group name |
| `--approve-provider-key-env` | credential **variable name** (never its value) |
| `--approve-provider-models` | full **ordered** model ids |
| `--allow-shell` | `LOCAL_SHELL` (host shell); without it shell calls are denied |

All five provider facts must match the resolved provider exactly; any missing fact
is `provider_approval_required`, any mismatch is `provider_approval_mismatch`, and
both are rejected **before credential resolution or network access** — so a
changed provider configuration cannot inherit a stale approval. The `.env`
layout keeps its existing semantics (model from `CAIRN_LLM_MODEL`, key variable
`CAIRN_LLM_API_KEY`, identity `default`).

Configuration and dotenv are read **from the target workspace**
(`<workspace>/.cairn/models.toml`, `<workspace>/.env`), not the process cwd. The
provider runtime is built through `build_provider_runtime`, so the real token
counter and model-fallback executor are used.

The shell runs as the invoking **server user** with that user's files and network
access; it is not sandboxed and does not claim network or filesystem isolation.
Declared credential variables are withheld from shell children. Configured
credential values are redacted from the result **and from the persisted JSONL
trace** (`RedactingTraceSink` scrubs span `error` text and string attributes), so
a provider/tool exception that echoes a secret cannot leak it.

## Machine-readable result

`--json` prints, and `--report` writes, this object (schema version 1):

```json
{
  "schema_version": 1,
  "task_id": "task-...",
  "status": "completed | budget_exhausted | runtime_error | cancelled | unauthorized | invalid_request",
  "verified": false,
  "workspace": "/srv/project",
  "final_response": "...",
  "trace_id": "...",
  "trace_persisted": true,
  "error_category": null,
  "error": null,
  "permission_denied": false,
  "denied_tools": [],
  "shell_authorized": false
}
```

`trace_id` is preserved for correlation even when the trace sink could not write.
`trace_persisted` reports whether the local JSONL trace was actually saved:
`true`/`false` when the turn reported it, `null` when it was not observed. A sink
failure yields a `trace_id` with `trace_persisted: false` and never exposes the raw
sink error (which could carry a credential).

`status == "completed"` means the single turn ended normally — **not** that the
change is verified correct; `verified` stays `false`. Failure, budget exhaustion,
cancellation, unauthorized launch and invalid input are distinct `status` values;
a completed run in which a tool was denied also sets `permission_denied` /
`denied_tools`. Entry validation errors (missing prompt, unreadable prompt file,
bad `--max-steps`) are reported as `invalid_request` in JSON mode too, not only as
plain text.

Exit codes: `0` completed, `1` runtime/config error, `2` invalid request,
`3` budget exhausted, `4` cancelled, `5` unauthorized, `6` completed with a denied
capability.

Cancellation: the library entry (`run_headless_task`) keeps propagating external
`asyncio` cancellation after owned cleanup. Only the CLI boundary translates a
`SIGINT`/`KeyboardInterrupt` (or cancellation) into a `cancelled` JSON result and
exit code `4`. A cancelled result preserves the observed trace id, captured from
the turn trace events, so the caller can locate the persisted trace at
`<trace-dir>/<trace_id>.jsonl`.

## Offline example

No provider or network is needed to exercise the entry end-to-end: the real
`CodingTaskRunner`/`run_turn` run against a scripted model in
[`tests/runtime/tasks/test_headless_task_entry.py`](../../tests/runtime/tasks/test_headless_task_entry.py)
(covers authorization, shell denial/grant, budget, cancellation, credential
redaction in result **and trace**, workspace-scoped config, bound provider
approval and machine-readable CLI validation/cancellation):

```sh
uv run pytest tests/runtime/tasks/test_headless_task_entry.py -q
```
