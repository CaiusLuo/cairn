# M2 Durable Task Ledger

[SessionStore](../../src/cairn/infra/sessions/store.py) persists Task lifecycle
facts alongside committed Sessions. The Store never executes a Turn, inspects
Git, reconstructs a WorktreeHandle or publishes to GitHub.
[CodingTaskRunner](../../src/cairn/runtime/tasks/runner.py) offers opt-in durable
execution through the existing `run_turn`; default stateless callers are unchanged.

## Host API

[Task DTOs](../../src/cairn/infra/sessions/task_models.py) are strict, frozen and
bounded. A Task is keyed by exact `(session_id, task_id)` and stores only the
SHA-256 digest of its UTF-8 input, its starting Session revision, its independent
Task revision and phase, a nullable verified checkpoint receipt, a nullable
result, fixed recovery diagnostics and an optional bounded host resolution. Task JSON is limited to 4 KiB; Task IDs
retain the existing 256-character reference bound, rejecting blank IDs and
characters below U+0020.
Known sensitive values and credential-shaped content are rejected by the
existing checkpoint codec on writes and reads.

The only forward phase sequence is:

```text
running -> turn_committed -> inspecting -> result_recorded
```

| Operation | Preconditions and result |
| --- | --- |
| `create_task(Revision, task_id=..., input_digest=...)` | Exact current, clean Session revision. Creates `running` at Task version 0; duplicate IDs in the same Session reject. |
| `commit_task_turn(TaskRevision, CheckpointReceipt)` | `running`; receipt must equal the verified current Session checkpoint and match the Task ID, input digest, starting checkpoint and original commit revision. Advances to `turn_committed`. |
| `start_task_inspection(TaskRevision)` | `turn_committed`; the Task checkpoint must still be the clean Session head. Advances to `inspecting`; performs no inspection itself. |
| `record_task_result(TaskRevision, TaskResultData)` | `inspecting`, still at the same clean Session checkpoint. Explicitly advances to `result_recorded`. A completed result requires a completed Turn outcome. |
| `require_task_recovery(TaskRevision, diagnostic)` | Any unfinished phase, exact revision; records `storage_failed`, `inspection_failed` or `interrupted` and blocks forward phase changes. Does not create a result or resolve a Turn. |
| `inspect_task(session_id, task_id)` | Read-only, no approval grant or mutation. Returns verified checkpoint references and bounded recovery facts. |
| `task_reconciliation(TaskRecoveryReport)` | Exclusive local recovery ownership. Rejects live Tasks/Turns and requires a clean, separately reconciled Session and an exact fresh report. Retains the writer until host work and cleanup finish. |
| `reconcile_task(TaskRecoveryReport, TaskResolution)` | Under that recovery guard, checks exact Task/Session CAS, identity, input digest and decision time. Abandons without a receipt, acknowledges the actual attributed receipt, or records authorization for a new inspection. Performs no inspection itself. |

Each mutation uses one SQLite transaction and an exact Task revision comparison;
it increments only the Task revision. Creation also checks the Session revision.
Failures roll back and use the existing Store fault latch and fixed error codes.
Concurrent writers are rejected by the existing lifetime writer lock. An uncertain
COMMIT requires inspection of actual stored facts; there is no mutation retry.

For an already securely opened, schema-2/3 Store, a trusted host can record facts:

```python
import hashlib
from cairn.infra.sessions.task_models import TaskResultData

task = store.create_task(
    store.inspect(session_id).revision,
    task_id=task_id,
    input_digest=hashlib.sha256(user_input.encode("utf-8")).hexdigest(),
)
# The existing Turn owner supplies a real settled CheckpointReceipt. Its
# TurnIntent must carry this exact task_id and input digest.
task = store.commit_task_turn(task.revision, checkpoint_receipt)
task = store.start_task_inspection(task.revision)
# The host performs its inspection separately, then explicitly records a result.
task = store.record_task_result(task.revision, TaskResultData(status="completed"))
```

`TaskResultData` is a deliberately small subset of runtime TaskResult: an outcome,
nullable 32-character hexadecimal trace ID and nullable historical repository
observation (`is_git_repository`, hexadecimal HEAD, dirty flag). No prompt, final
model response, arbitrary error text, tool arguments, paths or execution objects
are accepted. Unavailable observations stay null. Historical HEAD/dirty facts
provide no current Git evidence, checkout identity or publication authority.

## Opt-in CodingTaskRunner

The trusted host supplies a schema-2/3 writer Store, a fresh Agent with a fresh
permission handler, current Workspace/Git observations and freshly approved
provider configuration. It attaches that Agent with `DurableSession.create` or
`reopen`; a reopened Session must be explicitly reconciled using the current
exact report before admitting a new Task. See [DurableSession](durable-session.md)
for binding, credential and permission responsibilities. The runner neither
opens/upgrades storage nor resolves credentials or approves a provider.

```python
from cairn.runtime.tasks import CodingTaskRunner

# session is a ready, fresh host-supplied DurableSession. budget is its approved
# RunBudget; workspace and secret_env_keys are the current host configuration.
agent = session.agent
runner = CodingTaskRunner(
    workspace=workspace,
    llm=agent.llm,
    budget=budget,
    context_builder=agent.context_builder,
    permission_handler=agent.permission_handler,
    event_handler=agent.event_handler,
    tracer=agent.tracer,
    model_executor=agent.model_executor,
    secret_env_keys=secret_env_keys,
)
try:
    result = await runner.run(spec, durable_session=session)
    facts = session.store.inspect_task(str(session.session_id), spec.task_id)
finally:
    session.close()
```

`run(spec)` retains the stateless API and creates no Store. Durable execution
checks the exact Agent/configuration, empty permission grants, current host
binding, committed conversation and RunBudget before Task admission. Each Task
requires a fresh Agent/Session binding; prepare another fresh binding and
explicitly reconcile it for the next Task. Custom permission handlers and mutable
provider internals remain trusted-host responsibilities; saved references and
old grants authorize nothing.

1. Revalidate TaskSpec, hash its exact UTF-8 prompt, and atomically create
   `running` at the exact current Session revision before model/tool dispatch.
2. Call `DurableSession.task_execution(TaskRecord)` to bind the exact admitted
   Task ID, digest and base revision to one intended Turn. It rejects changed
   attribution or a second Turn; identity never comes from model text. The ordinary
   `run_turn(..., session=session)` owns admission, fences, cleanup and checkpoint.
3. Verify the actual receipt against that settled Turn and current clean Session,
   then call `commit_task_turn`. A committed Turn still leaves an unfinished Task.
4. Persist `inspecting`, perform fresh `RepoContextProvider.inspect_evidence`,
   validate current host bindings before and after inspection, then record only
   sanitized TaskResultData. Public TaskResult retains its
   response/status semantics; the ledger never stores its model output or error.

The local `SessionStore.task_execution(TaskRevision)` guard retains writer
ownership until post-turn inspection and owned cleanup settle. `session.close()`
requests shutdown during that scope; direct Store close rejects with
`task_still_owned`. Repeated caller cancellation cannot release either Task or
Turn ownership while cleanup is pending.

Cooperative cancellation can checkpoint a cancelled Turn, inspect current Git
and record a cancelled result. External cancellation propagates after cleanup;
if an unambiguous receipt exists, its Task may reach `turn_committed`, but no
inspection or result is manufactured. Failed inspection leaves `inspecting`
with `inspection_failed`; interrupted unfinished work uses `interrupted`.
Persistence failures use `storage_failed` when writable. If storage cannot be
read or written, incomplete records and the existing fault gate remain.

Unknown COMMIT acknowledgements are resolved by reading actual records without
retrying the failed mutation. A result may already be recorded even when the
caller receives a runtime error; inspect the facts before deciding what happened.
There is no automatic model, Task, Turn, inspection or mutation retry and no Task
resume. The built-in `read_file` primitive has a separate bounded retry for
explicit transient-before-effect local read failures; it cannot replay a Task or
another tool. Reopening and read-only inspection make no model/tool calls. Manual
Task reconciliation is described below; Session reconciliation alone never
advances Task phases or clears Task recovery diagnostics. See
[Safe retry](safe-retry.md).

## Explicit trusted-host Task reconciliation

`CodingTaskRunner.reconcile_task(session, report, resolution=...)` handles one
host decision. It never calls `run_turn`, a model, an Agent tool or a publisher.
The runner and Agent must still have the same freshly approved runtime,
Workspace, provider, budgets and permission configuration. Before recovering an
unfinished Task, use the existing exact `DurableSession.reconcile` procedure to
acknowledge the current Session binding and abandon any orphan Turn. That
separate decision preserves the orphan's fences and unknown-effect evidence.

Read a new `inspect_task` report after Session reconciliation. Construct a
`TaskResolution` from those exact Task/Session revisions and input digest, a
bounded host actor, a UTC decision time, and one action:

| Observed facts | Action | Result |
| --- | --- | --- |
| `running`, no committed Task Turn | `abandon_without_replay` | Persist a terminal abandonment decision. Keep phase `running`, no checkpoint/result, fixed recovery diagnostics and all unknown-effect evidence. Report `abandoned`; execution cannot resume it. |
| `running` with a committed Task Turn, or `turn_committed` requiring recovery | `acknowledge_checkpoint` | Verify the actual current receipt's digest, Turn, original commit revision, Task ID and input. Advance/retain `turn_committed`; clear its failure gate, retain the host decision, and perform no inspection. |
| `turn_committed` or `inspecting` with the current Task receipt | `inspect_repository` | Record the explicit decision at `inspecting`, validate current bindings, perform one bounded read-only Git inspection, then validate bindings again and record a sanitized result. |
| `result_recorded` or `abandoned` | Read stored facts | Return the verified report without writes, new inspection or Session activation, even after an uncertain COMMIT. `inspect_task` alone also suffices. |

An inspection decision does not acknowledge an unattached receipt; first make
the separate checkpoint decision and read its new Task revision. The inspection
result preserves the stored Turn outcome, including cancellation/failure, and
uses its nullable trace ID. Repository observations describe current host state;
they are not deterministic correctness verdicts or publication permission.

The shared Git inspector disables optional index writes and implicit promisor
fetches, and suppresses hooks/fsmonitor. Before status it rejects effective
clean/smudge/process filter configuration, including includes/worktree config,
and any tracked submodule. It requires a complete bounded index-mode listing
(64 KiB); unavailable objects, unsupported Git options, or incomplete inspection
leave recovery required. These refusals prevent repository-selected programs
from running during host inspection. They also apply to ordinary repository
context/evidence reads; there is no separate recovery Git executor.

```python
from datetime import UTC, datetime
from cairn.infra.sessions.task_models import TaskResolution

# Session was explicitly reconciled using its current host-observed binding.
report = session.store.inspect_task(str(session.session_id), task_id)
resolution = TaskResolution(
    task_revision=report.task.revision,
    session_revision=report.session_revision,
    input_digest=report.task.input_digest,
    action="acknowledge_checkpoint",
    actor="local-operator",
    resolved_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
)
report = await runner.reconcile_task(session, report, resolution=resolution)
# A later inspection requires its own explicit inspect_repository decision
# constructed from this newly read report. Saved resolutions never grant approval.
```

Task and Session revisions must remain exact; a changed Session head cannot be
used to finish an old Task. Refuse binding drift, stale approval, unknown identity
or inspection failure. An authorized inspection never retries itself. Failure or
cancellation leaves an unfinished Task with a fixed recovery diagnostic and no
manufactured result. Repeated caller cancellation cannot release Session/Store
ownership before the inspection's child/process cleanup settles. Closing during
recovery requests shutdown and defers Store closure until that boundary ends.

After an uncertain mutation COMMIT, return verified stored facts and retain the
Store fault gate. Do not retry the mutation, repeat inspection, or infer success
from a lost reply. If the result is present, it is returned unchanged. If absent,
stop and obtain new inspection/Session reconciliation/host approval before any
later action. The original Task and Session identities remain unchanged.

The SQL schema remains **3**, with no migration or checkpoint rewrite. Task JSON
accepts an optional `resolution` field; older records without it remain readable.
Ordinary Task writes omit it until a host decision exists. Older strict readers
that lack this contract reject reconciled records rather than discard host decisions. Original checkpoint
bytes, Task attribution and receipt hashes retain their meaning. Abandonment
uses a terminal decision in the bounded JSON rather than a new SQL phase.
Schemas 2/3 use the same Task table; this implementation does not relax platform
admission or qualify either schema for a new deployment.

## Schema compatibility and explicit upgrade

New databases use schema **4**, accepting both historical V1 full snapshots and
V2 incremental checkpoints. Task records and CheckpointReceipts keep their exact
meaning and bytes. Schemas 1/2/3 remain readable without migration; schema 1
still requires an explicit upgrade for Task APIs, and fencing a tool needs the
schema 4 fence table. Old Cairn versions that do not support schema 4 reject it;
there is no downgrade or reset operation.
See [SessionStore](session-store.md#codec-and-transactions) for the format matrix.

Upgrading is an explicit trusted-host operation, independent of Agent execution:

1. Stop every writer and wait for owned Task/Turn work and cleanup to finish.
2. Open the existing Store as a writer with the actual effective host-writable
   roots and known sensitive values, as described in [SessionStore](session-store.md).
3. Call `upgrade_schema()` once if `schema_version < SCHEMA_VERSION`; retain its returned
   backup path. An inspector, another writer or a locally owned Task/Turn cannot
   perform this operation.
4. Verify the schema version and committed Sessions before using incremental checkpoints.

For the standard durable CLI layout, the existing CLI Store opener computes the
Workspace, `/tmp`, effective temporary and uv cache roots:

```python
from pathlib import Path
from cairn.infra.sessions.models import SCHEMA_VERSION
from cairn.infra.workspace.workspace import Workspace
from cairn.terminal.durable import open_store

store = open_store(None, Workspace(Path.cwd()))
try:
    if store.schema_version < SCHEMA_VERSION:
        backup = store.upgrade_schema()
        print(f"Retained source-schema backup: {backup}")
    assert store.schema_version == SCHEMA_VERSION
finally:
    store.close()
```

Run from the actual Workspace. For a custom host configuration, pass its exact
store directory, `additional_roots`, `secret_env_keys` and `sensitive_values` to
`open_store`; do not omit writable roots or use stored provider grants. This
operation does not resolve credentials, make network requests or start an Agent.
There is no Task CLI or automatic CLI upgrade.

The upgrader holds exclusive writer ownership, verifies all Sessions and Task
receipt attribution, uses SQLite backup to include committed WAL pages, validates
the private backup and synchronizes it before migration. It retains the file as
`schema-<source-version>-backup-<random-id>.sqlite3` in the isolated Store.

One transaction creates the Task table when upgrading schema 1 and replaces the
checkpoint table with identical rows under the broadened format constraint. It
adds a per-Session Turn traversal index and copies payload blobs and digest
strings verbatim: Session/Turn/Task references,
commit versions and original receipts remain unchanged. Only new checkpoints use
V2.

The Store suspends SQLite FK enforcement solely under its exclusive effect
guard for the table rebuild, checks all foreign keys and semantic chains before
COMMIT, and restores enforcement after either COMMIT or rollback. Ordinary
mutations retain foreign keys ON. A pre-COMMIT failure rolls back to the original
schema and data; the source inode and backup are retained. There is no automatic
backup restoration or evidence deletion. The table copy needs temporary space
within the existing database limit; insufficient space fails closed.

On `commit_uncertain`, stop writes and inspect the actual schema and Sessions:
the migration may have committed. The backup remains at its original schema.
Do not retry blindly or copy it over a live database. Already-schema-3 calls
reject with `schema_upgrade_not_required` and create no extra backup. Inspection
and ordinary writer open never migrate. Writer open/close can still perform the
existing WAL maintenance.

## Recovery facts and limits

`inspect_task` verifies the entire committed Session chain and the Task's saved
receipt within one read transaction. It reports the current Session revision
and checkpoint, the Task phase/result, fixed diagnostics, and whether matching
Turn fences indicate that external effects may already have occurred. It exposes
no conversation payload, raw tool arguments or internal exception text.

An unfinished Task reports `incomplete`, or `recovery_required` when a stored
diagnostic, local persistence fault or Session recovery state requires it. The
report does not infer process death from a live phase. A committed Session Turn
alone leaves its Task unfinished, including a crash between Session commit and
Task receipt recording. Inspection and fresh Store reopening perform no work,
replay no tools and manufacture no result. Completed Task reports remain readable
when later Session checkpoints exist; further forward transitions require the
original Task checkpoint to remain current.

There is no Task execution-resume, automatic retry, scheduling, queue, DAG or
background worker. Session reconciliation keeps its existing exact trusted-host
procedure; only a separately supplied Task resolution can advance Task facts.

Real SQLite tests cover lifecycle/CAS, mismatched receipts, corruption, sensitive
content, read-only inspection, concurrent writer exclusion, migration backup and
rollback, uncertain COMMIT, and SIGKILL before/after Session and Task commits.
Task recovery tests add real temporary Git repositories and SIGKILL windows,
stale CAS, denied decisions, binding drift, unknown COMMIT and repeated cleanup
cancellation. Recovery preserves tracked bytes, Git HEAD/index and checkpoint
receipts without model calls or tool replay.
Process-death tests do not establish power-loss durability. The documented
[SQLite 3.50.4 WAL-reset release blocker](session-store.md#verification-and-release-gate)
remains unresolved; this slice does not upgrade SQLite or claim deployment support.
