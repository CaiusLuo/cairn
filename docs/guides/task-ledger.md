# Durable Task Ledger (M2-A)

[SessionStore](../../src/cairn/infra/sessions/store.py) persists Task lifecycle
facts alongside committed Sessions. It does not integrate
[CodingTaskRunner](../../src/cairn/runtime/tasks/runner.py), execute a Turn,
inspect Git, reconstruct a WorktreeHandle or publish to GitHub. Runtime Task
execution remains unchanged until a separate integration slice.

## Host API

[Task DTOs](../../src/cairn/infra/sessions/task_models.py) are strict, frozen and
bounded. A Task is keyed by exact `(session_id, task_id)` and stores only the
SHA-256 digest of its UTF-8 input, its starting Session revision, its independent
Task revision and phase, a nullable verified checkpoint receipt, a nullable
result and fixed recovery diagnostics. Task JSON is limited to 4 KiB; Task IDs
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

Each mutation uses one SQLite transaction and an exact Task revision comparison;
it increments only the Task revision. Creation also checks the Session revision.
Failures roll back and use the existing Store fault latch and fixed error codes.
Concurrent writers are rejected by the existing lifetime writer lock. An uncertain
COMMIT requires inspection of actual stored facts; there is no mutation retry.

For an already securely opened, schema-2 Store, a trusted host can record facts:

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

## Schema compatibility and explicit upgrade

New databases use schema **2**, adding one `tasks` table; Session tables and
checkpoint payload format **1** are unchanged. This version of Cairn continues
to inspect, reopen and write schema-1 Sessions, including through the durable
CLI, without migration. Task APIs reject schema 1 with `schema_upgrade_required`.
Older Cairn versions that accept only schema 1 reject a migrated database;
there is no downgrade or reset operation.

Upgrading is an explicit trusted-host operation, independent of Agent execution:

1. Stop every writer and wait for owned Turn work and cleanup to finish.
2. Open the existing Store as a writer with the actual effective sandbox-writable
   roots and known sensitive values, as described in [SessionStore](session-store.md).
3. Call `upgrade_schema()` once if `schema_version == 1`; retain its returned
   backup path. An inspector, another writer or a locally owned Turn cannot
   perform this operation.
4. Verify the schema version and committed Sessions before using Task APIs.

For the standard durable CLI layout, the existing CLI Store opener computes the
Workspace, `/tmp`, effective temporary and uv cache roots:

```python
from pathlib import Path
from cairn.infra.workspace.workspace import Workspace
from cairn.terminal.durable import open_store

store = open_store(None, Workspace(Path.cwd()))
try:
    if store.schema_version == 1:
        backup = store.upgrade_schema()
        print(f"Retained schema-1 backup: {backup}")
    assert store.schema_version == 2
finally:
    store.close()
```

Run from the actual Workspace. For a custom host configuration, pass its exact
store directory, `additional_roots`, `secret_env_keys` and `sensitive_values` to
`open_store`; do not omit writable roots or use stored provider grants. This
operation does not resolve credentials, make network requests or start an Agent.
There is no Task CLI or automatic CLI upgrade in M2-A.

The upgrader holds exclusive writer ownership, verifies all committed Sessions,
uses SQLite's backup API to include committed WAL pages, validates the private
schema-1 backup and synchronizes it before starting migration. The backup is
retained as `schema-1-backup-<random-id>.sqlite3` inside the isolated Store.
Adding the table, changing `user_version` and validating schema, foreign keys
and committed Session chains occur in one transaction. A pre-COMMIT failure
rolls back that transaction and retains the original schema, database inode and
committed data. No file replacement or automatic backup restoration occurs.
Ordinary writer opening and closing can still perform existing WAL maintenance.

On `commit_uncertain`, stop writes and inspect the actual schema and Sessions:
the migration may have committed. The backup remains schema 1. Do not retry
blindly or copy it over a live database. Already-schema-2 calls reject with
`schema_upgrade_not_required` and create no extra backup.

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

M2-A provides no Task reconciliation or execution-resume operation, TaskRunner
integration, retry, scheduling, queue, DAG or background worker. Session
reconciliation continues to use its existing exact trusted-host procedure and
does not advance Task facts or clear Task recovery diagnostics.

Real SQLite tests cover lifecycle/CAS, mismatched receipts, corruption, sensitive
content, read-only inspection, concurrent writer exclusion, migration backup and
rollback, uncertain COMMIT, and SIGKILL before/after Session and Task commits.
Process-death tests do not establish power-loss durability. The documented
[SQLite 3.50.4 WAL-reset release blocker](session-store.md#verification-and-release-gate)
remains unresolved; this slice does not upgrade SQLite or claim deployment support.
