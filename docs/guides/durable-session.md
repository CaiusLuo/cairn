# M1-C.2 DurableSession

[DurableSession](../../src/cairn/runtime/durable_session.py) opts one fresh Agent
into the existing [Session lifecycle](session-lifecycle.md) and
[SessionStore](session-store.md). `run_turn` remains the sole execution owner.
Stateless CLI, EvalRunner and CodingTaskRunner callers do no session storage I/O.
The [durable CLI](durable-cli.md) provides explicit local creation and recovery.
This API adds no Task recovery, replay or scheduler.

## Host API and authority

```python
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.loop import run_turn

# The trusted host builds a fresh, empty Agent, a dedicated writer Store outside
# ALL sandbox-writable roots, and an observed, approved SessionBinding.
session = DurableSession.create(store, agent=agent, binding=binding)
try:
    answer = await run_turn(agent, "new work", budget=budget, session=session)
    receipt = session.receipt
finally:
    session.close()

# In a new host lifecycle, use a fresh Agent and writer Store. Reopen only reads
# and restores committed messages: no model, tool, Git or observer dispatch.
session = DurableSession.reopen(
    new_store, binding.session_id, agent=fresh_agent, binding=current_binding
)
report = session.recovery_report
# HostResolution is a trusted human/caller decision about this exact report:
# its Session ID, unresolved Turn ID (or None), actor, time and current binding.
session.reconcile(report, host_resolution=host_resolution)
```

Create/reopen bind the exact Agent before attachment I/O. Empty history and no
stateless work in progress are required. Failed attachment leaves that owner
closed forever and leaves the supplied Store with the host for cleanup. An
omitted/wrong Session can never downgrade a bound Agent to stateless execution.

After successful attachment the DurableSession owns the dedicated Store's
writer lifetime. Do not share that instance between Sessions or close it behind
the owner. `session.close()` blocks new admission immediately; during execution
it defers Store close until work, cleanup, trace and lifecycle delivery settle.
It never cancels work or detaches the Agent. Idle close is idempotent. If storage
inspection/release fails, resources and the writer lock remain held; inspect or
retry close after storage is available. No force-close path exists.

The host supplies canonical Workspace/Git identities and approved provider
references. Runtime validates descriptor shapes/secrets, live path identities,
configured provider/group/IDs, prompt digest and budgets. Provider fingerprints
are host assertions about approved routing, not credentials or endpoint consent.
Runtime pins client, context builder, executor, permission handler and Workspace
collaborators; changes require explicit reconciliation. Do not mutate a pinned
client's internal endpoint/credential fields behind this API.

Git bindings require `observe_binding: Callable[[], SessionBinding]` at both
factories. This narrow synchronous host callback observes start/end metadata
(including HEAD/branch and stable registration identity) with bounded Git I/O;
it never approves replacement identities/providers. It is called at Turn
admission and settlement, **not** by reopen. The supplied reopen/resolution
binding is already host-observed. Non-Git hosts may omit the callback: runtime
revalidates the supplied identities and timestamps the settled observation.
An unavailable or replaced root/common/admin/registration blocks execution;
HEAD/branch or provider drift needs an explicit host acknowledgement. Settlement
can record tool-caused HEAD/branch drift on the same verified identity.

Use a fresh permission handler on reopen. Nonempty `SessionPermissionHandler`
grants are rejected during attachment/reconciliation. Custom callable handlers
are trusted host capabilities: the host must construct a fresh one and cannot
carry grants from an old process/session. No checkpoint restores permissions,
credential objects, provider runtimes or Worktree handles.

## Turn boundaries and observations

1. Exact ownership, open writer, ready/reconciled gate, current revision/binding,
   committed history prefix and input/record limits pass synchronously. Reserve
   the same Session guard; persist TurnIntent before trace/events/state/model work.
2. Use the ordinary loop and per-candidate ModelExecutor admission. Streaming
   deltas and partial calls stay provisional; only fully accepted responses enter
   AgentState. Recovery context is included in each candidate's request budget.
3. Each tool passes existence/argument preflight and **current** permission.
   `before_execute` then validates argument secrets/bounds and writes a ToolFence
   with monotone ordinal and assistant-message index. Only after the fence
   commits may the execution marker and tool run. Denied/malformed calls have no
   fence; fencing also applies to read-only tools. Fence errors escape ordinary
   tool-error conversion. Network authority still comes from granted capabilities.
4. Existing owned cleanup, rollback/pending-call closure and TurnTrace finalize.
   Its immutable summary contains nullable trace/usage/persistence and only the
   fixed `sink_failed` code. Raw trace payloads/exception text are never stored.
5. Validate full settled conversation and commit exactly once using Store CAS.
   Success and cooperative aborts, including empty rollback tails, can checkpoint.
   Compute the expected digest using the Store schema's encoding and the exact
   preceding receipt (V1 full payload or V2 tail plus predecessor), then match all
   receipt fields: Session/Turn, version/head, digest and outcome. The full
   materialized recovery view is not the bytes hashed by a V2 receipt.
6. Deliver `turn_settled`, then receipt-backed `turn_committed`, holding the guard
   throughout both. The latter's SessionEvent UUIDs and sequence attribute its
   checkpoint_seq, revision and outcome. Release faulted local Store ownership
   only after execution, cleanup, tracing and lifecycle delivery finish; then
   finish the Session guard and any requested close.

`agent_finish`, `trace_finish`, `turn_settled` and streaming events acknowledge no
durability. Live sequence counts attempted delivery, starts anew on reopen and
is not a stored cursor. Callback failure after settlement/commit does not change
the result, checkpoint or primary exception and never triggers replay.

Frozen `last_turn` flags identify settlement/commit delivery gaps. Callbacks must
remain synchronous and bounded, and must not spawn unowned asynchronous work.

## Recovery and failures

| Observation / failure | Conversation and execution gate |
| --- | --- |
| Clean create | Ready at checkpoint 0. |
| Clean reopen | Validated committed history only; Suspended until exact host resolution. No dispatch. |
| Unfinished intent/fence or local storage fault | RecoveryRequired; effects may exist. No new Turn until inspected and explicitly abandoned without replay. |
| Binding/runtime/history mismatch | Admission rejects without events/providers/state or durable mutation; suspended until host reconciliation. Invalid input alone can be corrected without reconciliation. |
| Admission/fence storage failure | Recovery gate latches. No later checkpoint may clear it; failed fence cannot execute. |
| Model/tool/budget failure or cancellation | Existing rollback/preserved facts and primary error survive. Closed, settled conversation commits with the interrupted outcome if storage/trace are sound. |
| Trace sink failure captured by Tracer | Checkpoint can commit with nullable usage and `persisted=False`, `sink_failed`. |
| Raised trace finalization/callback or missing finalized summary | No checkpoint. Keep intent, fixed recovery reason and live executed facts. Preserve primary failure/cancellation with a static note. |
| Checkpoint validation/SQLite failure | Retain live tool facts, block admission. Raise fixed SessionDurabilityError after otherwise successful execution; add only a static note to an existing primary error/cancellation. |
| Uncertain COMMIT acknowledgement | Inspect exact expected receipt after settlement; expose a confirmed identical receipt as metadata. No automatic acknowledgement event, retry or gate reopening. Explicit host reconciliation still required. |
| Failed recovery inspection/release | Keep local Store ownership/lock; once cleanup is proved complete an idle inspection can retry only the metadata release. No execution retry. |
| Post-receipt callback failure | Receipt remains authoritative; delivery flag records the gap. Next explicit Turn can execute once. |

Reconciliation requires an idle owner, an exact fresh RecoveryReport and typed
HostResolution; it cannot bypass a live Turn or cleanup. It restores only that
report's committed history, explicitly discarding any uncommitted live tail after
host assessment. It never synthesizes crashed tool results.

Abandoned attempts
produce bounded, sanitized host-built system context (count and up to eight
recent UUIDs) warning against old prompt/call replay; it is not saved conversation.
All restored observations are historical, requiring current repository/memory
freshness and new verification before publication. Exactly-once external effects
are not promised. Intent/fences/checkpoints do not authorize effects or approval.

## Verification and release limits

```bash
uv run pytest tests/runtime/test_durable_session.py tests/runtime/test_durable_session_recovery.py
uv run pytest tests/runtime/test_session.py tests/runtime/test_loop_recovery.py tests/runtime/test_loop_streaming.py tests/runtime/test_loop_tracing.py tests/infra/sessions
```

Tests use offline scripted clients, real SQLite, asyncio barriers and spawned
process lock/death probes. Existing stateless Loop/Tool/CLI/Eval/Task tests remain
compatibility evidence. SIGKILL proves application-crash behavior, not power-loss
durability. SQLite schema/codec/CAS and nonmutating inspection are unchanged.

Local SQLite **3.50.4** remains a **release blocker**; integration does not upgrade
or claim deployment support. The [Store release gate](session-store.md#verification-and-release-gate)
requires a fixed SQLite runtime and target macOS/Linux filesystem/locking
qualification; the store refuses an unfixed runtime at construction unless the host
declares it. See the [support matrix](../performance/sqlite-runtime-support.md).
Storage is synchronous and cleanup cooperative; an uncooperative
tool/provider or blocking callback can delay settlement. Task recovery and
Worktree adoption remain M2.
