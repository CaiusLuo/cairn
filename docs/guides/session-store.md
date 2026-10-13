# SessionStore and checkpoint codec (M1-C.1)

The concrete [SessionStore](../../src/cairn/infra/sessions/store.py) persists host
facts in a separate local SQLite database. This slice supplies storage primitives
only. It neither attaches an Agent nor calls a model, tool, permission handler,
Git command or publisher. `run_turn`, tool execution, AgentState and CLI behaviour
are unchanged. [M1-B](session-lifecycle.md) owns the live Session/Turn;
[M1-C.2/M1-C.3](../design/durable-session-v1.md#staged-implementation-and-acceptance)
will integrate settlement/fencing and explicit recovery UI.

## Host API

```python
from pathlib import Path
from cairn.infra.sessions.store import SessionStore

store = SessionStore(
    Path.home() / ".local/state/cairn/sessions",
    mode="writer",  # "inspect" is read-only under a short shared read guard
    forbidden_roots=(workspace_root, effective_tmp_root, effective_cache_root),
    sensitive_values=known_credentials,
)
try:
    receipt = store.create(binding)
    revision = store.begin_turn(receipt.revision, intent)
    revision = store.fence_tool(revision, fence)
    receipt = store.commit_turn(revision, settled)
    report = store.inspect(binding.session_id)
finally:
    store.close()
```

The host supplies **every** sandbox-writable root, including additional roots;
an empty set or overlap in either direction is rejected. The example values are
host-selected, not model inputs. The Store does not resolve credentials, approve
a provider, inspect a Workspace, or adopt/clean up a Worktree.

[Storage DTOs](../../src/cairn/infra/sessions/models.py) are frozen, strict Pydantic
models with immutable nested fields. SessionBinding contains canonical UUID4,
root `(path, device, inode)`, explicit Git/non-Git observation and nullable provider
reference. Git includes repository/common/admin identities, prefix, registration
digest and nullable HEAD/branch. Provider references contain IDs, fingerprints
and budgets, never credential-bearing endpoints, objects, prompt text or grants.
Failed Git inspection must not be represented as observed non-Git.

| Operation | Result / precondition |
| --- | --- |
| `create(SessionBinding)` | Atomic Session and empty checkpoint 0; receipt at version 0 after filesystem synchronization. Duplicate identity rejects. Writer construction creates only an empty schema. |
| `inspect(session_id)` | Consistent read; immutable RecoveryReport with current Revision/binding, exact committed checkpoint/receipt and attempt diagnostics. Does not classify a live writer as interrupted. |
| `begin_turn(Revision, TurnIntent)` | Ready state, exact start binding/head/message boundary, fresh Turn ID and SHA-256 of user UTF-8 input. Intent only, version + 1; raw input is not in intent. |
| `fence_tool(Revision, ToolFence)` | Exact active Turn begun by this Store instance; monotone unique invocation reference. Fence only, version + 1. |
| `commit_turn(Revision, SettledTurn)` | Same live Turn, settled by runtime; exact prefix/input/binding/fence relationships. One checkpoint, terminal attempt and head/version advance atomically. Duplicate commit rejects. |
| `require_recovery(Revision, reason_code)` | Fixed `storage_failed`, `finalization_failed` or `binding_unavailable`; marks Session/active attempt and latches admission, version + 1. |
| `release_turn(Revision, turn_id)` | Trusted owner confirms a faulted local Turn's execution **and all cleanup/finalization have ended**. Exact current Revision and owned Turn required, including a possibly committed receipt after uncertain COMMIT. Local-only: changes no rows/revisions and grants no admission. Invalidates prior inspection. |
| `reconcile(Revision, HostResolution)` | Exact revision from explicit successful inspect; typed host `abandon_without_replay` only. Rejects any still-owned Turn in that Session, including during cleanup or after a write fault. Abandons exact unresolved Turn or acknowledges a clean binding. Retains history/fences; version + 1, head unchanged. |
| `close()` | Writer rejects locally owned Turns with fixed `turn_still_owned` before closing SQLite or either lock; preserves ownership/connection/locks without latching a fault. Successful terminal commit or explicit faulted release permits close. SQLite then closes under the effect guard; busy guard retains resources. Inspector close and successful repeated close remain ordinary cleanup. Never unlinks stable locks. |

Revision contains Session ID, version and head sequence; receipt adds Turn ID,
outcome and payload SHA-256. The original commit version remains in the attempt;
later reconciliation cannot rewrite a receipt. Task IDs remain optional host
strings scoped by Session, with a 256-character storage bound. Root/Git registration
identity cannot change. Host reconciliation can approve provider/HEAD/branch
changes without rewriting historical checkpoints. Stored bindings cannot approve
an endpoint or prove checkout freshness.

## Codec and transactions

[Codec](../../src/cairn/infra/sessions/codec.py) uses schema **1** and independent
payload format **1**. Checkpoints contain full conversation, never ContextBuilder's
trimmed requests. `snapshot_messages` detaches core messages and stores arguments
as canonical JSON object text (`arguments_json`), making nested arguments immutable.
`restore_messages` revalidates and returns new core Message/ToolCall objects.
No migration, reset, legacy adapter or older-head fallback is implicit.

Canonical UTF-8 JSON has sorted keys, compact separators and unescaped Unicode.
Writes and reads reject unknown/duplicate fields, noncanonical encodings,
nonfinite numbers, invalid identity/time/range/outcome and malformed tool groups.
Exactly one result follows each call immediately after its assistant group, in
call order. Call IDs are group-unique; reuse in later groups is legal. Every new
nonempty tail begins with its sole user input. Completed tails end in a final
assistant; interrupted tails can end in a closed tool group or be empty after
rollback. Previous committed history is an exact unchanged prefix. SHA-256 detects
damage, not malicious trusted-host forgery. `model_copy` bypasses are revalidated.

The accepted schema has `sessions`, `turns`, `checkpoints`, deferred head/active/
terminal foreign keys, unique Session/Turn checkpoint and one unresolved attempt
per Session. Bounded `diagnostics_json` stores start binding/message boundary,
ordered fences, fixed reason, nullable resolution and commit version; no tool
arguments/results or replay position. UTC describes observations; revisions and
sequences determine order. Readers validate schema shape/version, quick-check,
foreign keys, exact head and the full committed prefix/attempt chain. Corruption
or unsupported state raises fixed `SessionStoreError.code`, preserving files.

Each mutation prevalidates/encodes payloads, including combined diagnostics,
before `BEGIN IMMEDIATE`; then rereads state/binding/active identity and CAS-updates
with Session ID, expected version and head. Failure rolls back every row.
WAL, FULL synchronous, foreign keys ON, trusted schema OFF and 2,000 ms busy
timeout are checked. A writer keeps one connection; an inspector opens at most
one connection per guarded call and closes it before releasing the guard. Both
are confined to their constructing thread/process. No background writes or
mutation retries exist.

| Bound | Behaviour |
| --- | --- |
| Checkpoint 8 MiB; 10,000 messages | Inclusive limits; no pruning. |
| Message/argument encoding 1 MiB | Both apply; escaping/envelope overhead can make message bound bind first. |
| 256 calls/fences per turn; diagnostics 64 KiB | Both apply; long references can exhaust diagnostics first. |
| Database 256 MiB | File and SQLite max-page-count checks; no evidence deletion. |
| WAL admission 32 MiB | At/above threshold blocks writes unless idle maintenance reclaims it. One admitted transaction can cross it; this is not an instantaneous cap. |

Idle bounded TRUNCATE maintenance follows create/settlement/reconciliation and
writer open. Busy/I/O failure sets `maintenance_failed`, never revokes a successful
receipt. A retained reader can block reclamation; admission checks WAL again.
Maintenance and the post-COMMIT receipt boundary are separate.

Intent/fence admission reserves diagnostic capacity for settlement or host
abandonment, including bounded actor/note and HEAD/branch drift with the start
provider reference. Thus a fence cannot fill the record and prevent recovery.
A larger replacement provider reference must also fit; acknowledge it separately
on a clean Session if needed. Reserved accounting is never stored as a resolution.

## Recovery and security

| Observation / failure | Contract |
| --- | --- |
| Clean committed head | Restore facts only; live attachment/approval belongs to M1-C.2. |
| Executing intent without fence | May be live or interrupted; no inferred completion/resubmission. |
| Fence | External outcome unknown, even when committed before tool entry. |
| Recovery-required / local write fault | Normal writes blocked until fresh inspect and exact host resolution. |
| Uncertain COMMIT acknowledgement | Raise `commit_uncertain`; inspect exact Turn/receipt to distinguish old versus committed state. Never automatically retry settlement/execution. |
| Bad/missing head, digest, JSON, FK or schema | Fail closed; no older checkpoint promoted as recovered head. |

Mutation failure latches writes while **retaining** live attempt ownership.
`require_recovery` and inspection are not evidence that execution has ended.
Rejected live reconciliation leaves ordinary owned execution usable; it does not
latch a new fault. Another Session's reconciliation cannot clear that Session's
fault. Reopened writers lack ownership of pre-existing attempts. Host abandonment
retains evidence and manufactures no tool results, kill authority or replay
permission. The host must inspect possible effects/surviving work before constructing HostResolution;
decisions must not come from model text. A later normal checkpoint cannot clear
a fault. New human-requested work cannot be proven semantically different from
an earlier mutation; exactly-once external effects are not promised.

After a local storage/finalization fault, the trusted owner must finish owned
work using the existing runtime settlement boundary, then call `release_turn`,
inspect again and explicitly reconcile. Releasing ownership alone never clears
the fault or abandons the SQL attempt. Successful settled commit already releases
ownership. M1-C.2 must place the faulted release after owned model/tool cleanup,
conversation settlement, tracing and lifecycle delivery; an exception handler or
recovery UI must not release early. The Store cannot prove that asynchronous work
has stopped. Writer close enforces that no locally owned Turn remains, including
through cancellation cleanup or after a write fault. Completed cleanup alone does
not remove ownership; use terminal commit or the explicit faulted release.
Abrupt process termination releases the kernel-held lock and leaves the existing
orphan inspection/recovery contract intact. Closing a database is not cancellation.
This slice adds no runtime integration or force-close path.

[Path handling](../../src/cairn/infra/sessions/paths.py) walks components using
descriptor-relative O_NOFOLLOW, creates 0700 directories/0600 files, rejects
symlinks/hardlinks/type/mode changes and rechecks directory/lock descriptors and
ancestor/DB/sidecar path identities before mutations and COMMIT. SQLite owns its
DB descriptors; inspection never opens/closes an extra raw DB fd that could drop
another connection's POSIX locks. Supplied writable-root aliases must still resolve
to their original targets. macOS uses statfs64/MNT_LOCAL; Linux accepts known local
ext/XFS/Btrfs/tmpfs/overlay/ZFS/F2FS filesystems. Unsupported
platform/filesystem/locking fails closed. Nonblocking exclusive `flock` on stable
`writer.lock` lasts until close across all Session IDs; no PID/expiry stealing or
stale-lock deletion. Inspect creates no absent database/schema and never falls
back to writable access.

Inspection uses a separate stable 0600 `inspection.lock`. Only a writer may
create it; inspectors open it O_RDONLY and take nonblocking shared `flock`, never
the lifetime writer lock or an exclusive lock. Every Store SQLite operation,
including writer open, transaction/rollback, maintenance, reads and close, takes
the corresponding exclusive guard. This serializes short inspection epochs
against SQLite effects while the live execution/writer ownership remains intact.
Contention fails closed with `store_busy`, without implicit retries. Mutation
contention follows the existing fault latch; an idle inspector cannot pin a
SQLite snapshot or block later reclamation.

Under that guard, inspection selects **before any database read**:

- With no WAL, `mode=ro&immutable=1` reads the pinned main database. The guard
  prevents writer changes for the entire connection lifetime; it is never an
  immutable connection on an unprotected changing source. SQLite reports a
  connection-local DELETE journal mode here; writable opens still enforce WAL.
- With any WAL (including an empty one), `mode=ro&vfs=unix-none`, EXCLUSIVE
  locking mode before the first read and `query_only=ON` build the WAL index in
  heap memory. This reads committed WAL frames without mapping/writing SHM.
  Checked `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE` prevents checkpoint or WAL deletion
  on inspector close. The application guard supplies the locking intentionally
  omitted by that VFS. Each call opens afresh, so no stale cached WAL index survives.

This follows SQLite's [WAL without shared memory](https://www.sqlite.org/wal.html#noshm)
and [no checkpoint on close](https://www.sqlite.org/c3ref/c_dbconfig_defensive.html#sqlitedbconfignockptonclose)
contracts. Ordinary `mode=ro/query_only` can create sidecars, and `readonly_shm=1`
can reuse a writable SHM node in the same process; neither is the inspection
contract. No custom VFS, raw DB descriptor, snapshot copy or writable fallback
is introduced. Missing/unsafe guard, unsupported VFS/configuration, rollback
journal or unavailable locking rejects inspection without changing source files.
An older store lacking the guard needs one explicit trusted writer open/close.
All clients must use this guard protocol; mixed-version writers, direct SQLite
clients and host file replacement are unsupported. Read access may update OS
atime; the guarantee covers file set, contents, identities, permissions and
modification metadata, not filesystem access accounting. macOS/Linux deployment
qualification of these VFS/flock semantics remains required.

Known secrets and conservative credential patterns are checked on decoded strings,
nested arguments and canonical encoding before mutation. Reject entire records;
never redact or log payloads/raw exceptions. Patterns can reject ordinary code
and miss unknown/encoded secrets; hosts must supply known values. Private modes
protect other users. Same-user hostile races/direct SQL and transcript confidentiality
are outside this guarantee. Hosts must not replace/move open stores or run one
Workspace through multiple stores. SQLite cannot atomically commit external effects.

## Verification and release gate

```bash
uv run pytest -q tests/infra/sessions
uv run pytest -q tests/core/test_architecture.py tests/docs tests/infra/memory/test_store.py tests/infra/workspace/test_workspace.py tests/infra/git/test_security.py
```

Real SQLite tests use spawned child/Pipe barriers, SIGKILL at seven named windows,
faults after checkpoint INSERT/attempt UPDATE, busy/I/O/unknown COMMIT, security
rejection and exact bounds. Held work/cleanup barriers reject live reconciliation
under repeated cancellation; uncertain COMMIT tests retain local ownership even
when SQL already contains the terminal receipt. Real cold/live/crashed-WAL reads
compare filesystem state, with spawned writer barriers testing both guard directions.
The file-mutating effect double flushes and fsyncs before its crash barrier.
SIGKILL tests application crashes, **not power-loss durability**. Deployment
filesystems/device synchronization still need validation.

Local Python 3.12.13 uses SQLite **3.50.4** on macOS with flock. This is a **release
blocker**, not a supported deployment claim. The reviewed
[SQLite WAL-reset fix](https://www.sqlite.org/wal.html#walreset) requires 3.51.3+,
3.50.7/3.44.6 backports or documented equivalent. `sqlite_wal_fix_supported()` exposes
the version check. This isolated development slice does not upgrade or disable
the project runtime. Compatible SQLite and target-platform tests are required
before shipping durable execution. M1-C.2 integration, Task recovery, automatic
replay, queue and DAG are outside this slice.
