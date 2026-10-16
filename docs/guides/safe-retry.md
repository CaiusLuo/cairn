# Safe retry

Cairn has one automatic retry slice: the built-in `read_file` tool may repeat a
failed local read when the operating system reports a narrowly recognized
transient error and no result has crossed the tool boundary. This is not a
general retry framework. The Agent, model output and arbitrary tool adapters
cannot opt an operation into retry.

## Typed failures

`ToolFailureKind` distinguishes the facts the harness knows about a failed tool
operation:

| Kind | Meaning | Automatic retry |
| --- | --- | --- |
| `transient_before_effect` | The owning implementation proved that no external effect occurred and the failure may clear | Only inside the built-in `ReadFileTool` |
| `permanent` | Validation or the operation cannot succeed unchanged | No |
| `permission_denied` | Policy, user or host authority rejected the operation | No |
| `cancelled` | Caller cancellation interrupted owned work | No; propagate cancellation |
| `deadline` | The operation's shared deadline expired | No |
| `effect_unknown` | The harness cannot prove whether an effect occurred | No; retain the failure fact |

`ToolFailureOutcome` carries a sanitized type and message with that kind.
`ToolExecutionFailure` transports it across the existing Tool interface. Generic
tool execution records the kind on its existing failure events and execution or
preflight spans, but never treats the type as retry authority. This keeps a
custom or buggy adapter from requesting a repeat by labelling its own failure
transient.

## Ownership

The retry remains inside `ReadFileTool`. That implementation alone knows that a
partial local read produces no write, process, network or publication effect and
that no partial bytes become a `ToolResult`. `runtime.tool_execution` still owns
one logical tool call: preflight, permission, durable admission, one execution
span and one final model-facing tool message.

The surrounding owners do not replay work:

- `ModelExecutor` keeps its existing bounded candidate fallback. It does not
  retry one provider request, and shared quota, timeout and 503 failures still
  stop.
- `RunBudget.max_steps` still counts Agent steps, not internal read attempts.
- `CodingTaskRunner` does not repeat a Turn, tool call, repository inspection or
  persistence mutation.
- Bash, Git operations, file writes, GitHub publication and every
  `effect_unknown` failure remain single-attempt operations. Publication keeps
  its existing attempt claim and manual reconciliation requirements.

## Bounds

One `read_file` invocation uses all of these independent bounds:

| Bound | Maximum |
| --- | ---: |
| Attempts, including the first | 3 |
| Shared retry deadline | 0.25 seconds |
| Cumulative backoff budget | 0.03 seconds |
| Backoff sequence | 0.01, then 0.02 seconds |
| Backoff per wait | 0.02 seconds |

Only `EAGAIN`, `EINTR`, `EBUSY` and `ESTALE` are transient. Permission failures,
missing files, invalid UTF-8 and other I/O errors do not retry. The same monotonic
deadline covers every attempt and delay. The reader checks it before the read,
after the read and between bounded 4 KiB fragments. A blocking kernel filesystem
call cannot be preempted portably; the deadline prevents another attempt after
that call returns and bounds harness-controlled scanning and backoff.

Cancellation during backoff propagates immediately and starts no further read.
Deterministic tests inject a monotonic clock and transient file-open failures to
prove the attempt, deadline and wait-budget limits without sleeping. Runtime
tests prove retries still produce one permission decision, execution span and
tool fact, while an effectful custom tool remains single-attempt even if it emits
a typed transient failure.
