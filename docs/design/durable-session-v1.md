# Durable Session V1

Status: proposed; independent architecture review required before implementation.
Scope: [Issue #31](https://github.com/CaiusLuo/cairn/issues/31), single local
process, opt-in CLI/Agent sessions; future M1-C persistence subdesign.
Source audit baseline:
`52d75fa2124dba6801e7139f74db7ac229ba04f1` (latest main at design time).
[Architecture](../architecture.md), [ADR 0001](../adr/0001-modular-architecture-baseline.md)
and [AGENTS.md](../../AGENTS.md) remain canonical.

## Decision in brief

Persist a full, validated conversation only after the existing `run_turn` has
settled execution, applied its rollback/abort policy and finalized tracing.
Before execution starts, persist a turn intent; immediately before each tool
execution, persist a conservative effect fence. These small diagnostic records
are **not resumable checkpoints**. Reopening restores the last committed
conversation and surfaces any unfinished attempt separately. It never dispatches
a model, tool, Git operation or publisher.

Use one concrete runtime `DurableSession` and one concrete infrastructure
`SessionStore`. SQLite transactions, a session revision CAS and a process-lifetime
local writer lock protect the store and execution admission. No event log,
scheduler, second Agent loop or automatic replay is introduced. SQLite atomicity
covers its rows; it cannot atomically commit a filesystem or remote side effect.

V1 persists completed and cooperatively interrupted **settled** turns, including
a turn that rolled back before tool execution. A process-interrupted attempt
cannot be promoted to a checkpoint, even if diagnostic output looks complete.
A host must reconcile and abandon it before starting a new turn.

## Roadmap dependencies and streaming boundary

[Roadmap V2](https://github.com/CaiusLuo/cairn/issues/24#issuecomment-6098692888)
sets the order below. Designing M1-C now does not authorize skipping its
predecessors or coding the Store slices before M1-A/M1-B pass their milestone
acceptance gates.

| Stage | Prerequisite contract / scope |
| --- | --- |
| M1-A — Streaming | Bounded response/tool-call assembly, cancellation and no partial tool execution; define the provider/LLM streaming API. |
| M1-B — Session Lifecycle | Session/Turn identity, bounded ordered event delivery and binding/close/cancellation rules; finalize the Agent admission hook used below. |
| M1-C — SessionStore | This document: committed-turn persistence and manual conversation recovery, implemented only after M1-A/M1-B milestone acceptance. |
| M2 — durable Task recovery | Separate TaskRun state/checkpoint/recovery design owned by the Task runner; consumes the settled-turn boundary, not streaming deltas. |

Current [LLMClient.generate](../../src/cairn/core/llm.py) returns one LLMResponse;
[Event](../../src/cairn/core/events.py) has no token-delta contract. M1-A/M1-B own
those changes. Persistence consumes their accepted completion/lifecycle seam:

- Streaming text and tool-call fragments are provisional, bounded per-attempt
  assembly data; SessionStore persists no deltas or assembly buffers. While a
  response is incomplete, only its turn intent and any fences from earlier
  accepted responses may already be durable. Only an explicitly completed, fully
  assembled and validated response may enter AgentState at the existing
  response-acceptance point.
  Parseable argument JSON or one finished call in an incomplete group is not
  permission to dispatch a tool. Never mix fragments from fallback attempts.
- Cancelling an incomplete response discards its assembly buffer and dispatches
  none of its tools. After owned stream work/cleanup settles, the existing turn
  abort policy applies: a checkpoint may record a settled interruption containing
  only previously accepted facts (or an empty rollback range), never partial
  deltas/calls. Process death still leaves an unfinished intent for manual recovery.
- Durable-session stream events carry Session/Turn/attempt identity and order
  under M1-B delivery rules. Emitted tokens, `agent_finish` and `trace_finish` are
  provisional with respect to durability; their consumers cannot advance
  Turn/Task state, execute tools or write checkpoints. `run_turn` retains
  settlement ownership.
- For durable turns, a distinct `turn_committed` lifecycle notification carries
  Session/Turn IDs, checkpoint sequence/revision and settled outcome **only after**
  the checkpoint receipt. It acknowledges storage, not Task correctness or normal
  completion. Missing/failed acknowledgement delivery is a separate diagnostic:
  preserve the committed outcome/primary cancellation, do not re-enter rollback
  or replay, and inspect the receipt by Turn ID on reopen.

## Current owners and evidence

| Concern | Current source and observed contract | V1 ownership |
| --- | --- | --- |
| Conversation | [AgentState](../../src/cairn/core/state.py) has only `messages`; [Message/ToolCall](../../src/cairn/core/models.py) are permissive Pydantic models. [Agent](../../src/cairn/runtime/agent.py) holds the state. | Agent remains the sole live conversation owner. Session metadata and storage DTOs do not become AgentState fields. |
| Turn | [run_turn / _settle_aborted_turn](../../src/cairn/runtime/loop.py) sequences attempts, tracks pending calls, rolls back before execution, otherwise preserves facts and closes pending calls with failures. | `run_turn` alone chooses when the turn is settled and its outcome. DurableSession supplies storage admission and records that decision. |
| Tool boundary | [execute_tool_call](../../src/cairn/runtime/tool_execution.py) validates before approval, derives authority from granted capabilities, records a tool message before result events. A tool can mutate and then raise. | Same owner; add a fence after permission approval and before `mark_tool_execution_started()` / `tool.execute()`. |
| Trace | [TurnTrace.close](../../src/cairn/runtime/turn_trace.py) finalizes usage and `trace_finish`. [Tracer.end_span](../../src/cairn/infra/observability/tracer.py) captures sink errors; an event-handler exception can still escape. | TurnTrace remains telemetry owner. Persist its small summary, never reconstruct conversation from JSONL. |
| Session bindings | [ProviderSession](../../src/cairn/runtime/provider_runtime.py) commits active provider bindings; [SessionPermissionHandler](../../src/cairn/core/permissions.py) holds live grants. | DurableSession owns identity, revision, active-turn metadata and reopen gate; it does not own provider switching or permission decisions. Grants never survive reopening. |
| Task | [CodingTaskRunner.run / _execute / _turn](../../src/cairn/runtime/tasks/runner.py) builds a fresh Agent per Task, invokes the same loop, then inspects repository evidence. [TaskResult](../../src/cairn/core/tasks.py) is not a verification verdict. | Runner retains Task lifecycle. V1 records an optional Task ID as a link only; durable Task results are a later milestone. |
| Workspace / Worktree | [Workspace](../../src/cairn/infra/workspace/workspace.py) is a path boundary. [WorktreeHandle](../../src/cairn/infra/git/worktree.py) owns registration, retention and release. [LocalWorkflow._execute](../../src/cairn/runtime/workflow/runner.py) retains its handle immediately after creation. | Persist provenance as observations, never a live handle or cleanup authority. SessionStore cannot retain, release or adopt a Worktree. |
| SQLite / reports | [MemoryStore](../../src/cairn/infra/memory/store.py) already uses private paths, WAL, FULL sync, schema checks, bounded transactions and version CAS. [persist_report](../../src/cairn/runtime/workflow/models.py) uses fsync and atomic replacement. [GitHubPublisher._claim_publication](../../src/cairn/runtime/workflow/publisher.py) deliberately retains a publication-attempt claim. | Reuse the safety patterns in a separate store; do not expand MemoryStore, treat a report as a checkpoint, or clear a publication claim. |

Existing recovery tests demonstrate write-then-fail, later provider failure,
permission failure, budget exhaustion and event-handler failure:
[test_loop_recovery](../../tests/runtime/test_loop_recovery.py).
[Trace tests](../../tests/runtime/test_loop_tracing.py) distinguish missing trace
persistence from failed execution. [Task tests](../../tests/runtime/tasks/test_runner.py)
pin fresh Agents and cancellation during execution/inspection.

## Identity and stored truth

Session and Turn IDs are host-generated UUID4 strings; they are distinct from
Trace IDs. Preserve `TaskSpec.task_id` as the caller's existing nonempty string,
scoped by Session ID; do not change its type or assume global uniqueness.
Sequence zero is an empty initial checkpoint. Each settled turn adds exactly one
checkpoint (`seq + 1`); abandon/reconcile operations advance the session
`version` but never checkpoint sequence. UTC timestamps describe observation,
not ordering or lock expiry. No message ID is added to core: snapshot message
index gives order; the host assigns each tool invocation a turn-local ordinal.
Provider ToolCall IDs are preserved and scoped to their assistant-message group,
so reuse across groups/turns is legal.

A checkpoint envelope (`format_version=1`) contains:

- Full `AgentState.messages`, not the ContextBuilder's trimmed request; the
  settled turn's `[start, end)` message range may be empty after rollback.
- Session/Turn IDs, sequence, outcome (`completed`, `cancelled`,
  `budget_exhausted` or `runtime_error`), start/settle timestamps.
- Host-observed Workspace binding: canonical root and `(st_dev, st_ino)`,
  plus Git repository root, common/admin directory paths and identities,
  workspace prefix, registration/backlink digest, HEAD and branch when present.
  Non-Git bindings are explicit; a failed Git inspection is not non-Git.
- A host-supplied provider/model **reference**: provider name, model group/IDs,
  credential-free endpoint/config fingerprint, system-prompt fingerprint and
  context/run budgets. This is the configured binding, not an assertion about
  which fallback candidate answered every request. Actual attempts remain trace
  facts. Direct provider-neutral hosts may supply a null reference.
- Trace ID, trace-persisted boolean and usage counters, each nullable when
  unavailable; a fixed trace error code rather than raw sink exceptions.

Checkpoint 0 has null Turn ID/outcome, an empty message list/range and null trace
fields. The session row's binding is the currently admitted host binding; the
checkpoint binding remains its historical settle-time observation. Begin records
the approved start binding in attempt diagnostics; commit records the end binding
and updates the session row. Reconciliation/provider switches can change the
admitted reference without rewriting a checkpoint. Root/registration identity
must remain the same; HEAD/branch changes made by an executed tool are observations,
not grounds to discard its facts. The runtime performs fresh read-only identity
inspection before admission and after settlement, outside SQL transactions.

Do not persist API keys, credential environment values, provider/client objects,
full provider configuration, approval tokens/grants, raw LLM response objects,
system prompt text, generated repository/memory system messages, or arbitrary
exception representations. Stored message contents and arguments remain
untrusted conversation data; validation of the envelope does not make them host
instructions, permission grants, freshness evidence or Task success.

Validate at write **and** restore, including host-created `model_copy` values:

1. Strict known fields/types; no duplicate JSON keys, NaN/infinity, extra keys or
   unsupported versions. Reconstruct Message models only after strict DTO checks.
2. History contains only user/assistant/tool messages. Each user starts a turn;
   accepted assistant tool groups have nonempty, group-unique call IDs and names.
   Exactly one tool message per call, in call order, follows that group before
   another assistant/user message. Reject orphan, duplicate or dangling results.
   Interrupted turns can end with a closed tool group without a final assistant.
3. Envelope IDs/sequence, turn ranges and SQL columns agree. Preserve the previous
   committed history as an exact prefix; only the new turn tail can change.
   Canonical UTF-8 JSON plus SHA-256 detects damage, not malicious host forgery.
4. Check known credential values and credential-like patterns on the decoded
   strings as well as canonical encoding. Reject the whole write, without
   redaction, truncation, payload logging or alteration of live tool facts.
   Precheck user input before turn admission and arguments before their fence.
5. Proposed bounds: 8 MiB per checkpoint, 10,000 messages, 1 MiB per individual
   message/argument encoding, 256 tool invocations per turn, 64 KiB per attempt
   diagnostic record and 256 MiB database. A 32 MiB WAL admission threshold
   blocks further execution/writes until bounded idle maintenance reclaims space;
   it is not a maximum instantaneous WAL size. Reaching a bound fails explicitly;
   V1 does not prune history or delete recovery evidence.

Secret-pattern detection is imperfect and can reject ordinary coding text.
The host must supply known credential values. Durability failure after a tool
returned sensitive output blocks continuation and preserves the earlier
checkpoint/fence; it must not store that output in a fallback report.

## State transitions and recovery

Session state is a durable execution gate. Store reads do not change state; only
the runtime holding the writer lock can advance it. A fresh process must never
interpret `executing` as a permit to continue an old invocation.

| Session before | Trigger / precondition | Session after | Conversation / admission |
| --- | --- | --- | --- |
| Absent | Explicit create, safe store and approved host binding | `ready` | Empty checkpoint 0, no calls. |
| `ready` | Trusted host confirms a clean reopened binding/report revision | `ready` | Advance revision and open the live attachment gate; no checkpoint or call. |
| `ready` | `run_turn` admits a new Turn under expected revision | `executing` | Intent committed before adding the user message or calling a provider. |
| `executing` | Tool passed preflight/permission; effect fence commits | `executing` | Monotone fence added; only this live invocation can now execute. |
| `executing` | Fully settled success or cooperative abort; checkpoint transaction commits | `ready` | Full state and terminal Turn status become visible together. |
| `executing` | Process death, unresolved write outcome or failed finalization/persistence | `recovery_required` | Keep last checkpoint; stop admission. If storage is unavailable, this state is latched in memory and inferred from the durable intent on reopen. |
| `ready` | Reopen detects binding mismatch/unavailable identity | `recovery_required` | Inspection only. No automatic retargeting of a moved/replaced Workspace or clone. |
| `recovery_required` | Trusted host reconciles exact report revision and live binding | `ready` | Abandon old attempt, retain diagnostic metadata. No new checkpoint and no call dispatch. |
| Any | Corruption/unknown schema or format | No transition | Refuse runnable restoration; retain files. A safe read-only diagnostic is allowed if independently decodable. |

Turn status distinguishes settlement from process interruption:

| Turn transition | Required evidence | Checkpoint? |
| --- | --- | --- |
| New → `executing` | Intent tied to current head/revision | No |
| `executing` → `completed` | Normal loop exit, tool groups closed, tracing finalized | Yes |
| `executing` → `interrupted` | Existing loop abort/rollback and owned cleanup finished; tracing finalized | Yes, even if state equals prior checkpoint |
| `executing` → `recovery_required` | New process owns lock but finds unfinished intent, or live durability fault | No |
| `recovery_required` → `abandoned` | Host reconciliation bound to Turn ID and session revision | No |
| Terminal → `executing` | Forbidden | A new Turn must use a new ID |

Reopening is explicit and read-only until the host confirms the inspected
Workspace/provider binding; creating a new permission handler is mandatory.
Stored credentials/config cannot choose or approve an endpoint. Missing or
changed model references require a newly built, approved current runtime, with
matching token counter/admission; no fallback to a remembered secret or catalog.

Reinspect root, Git context, registration and HEAD. A replaced root/common/admin
directory, changed registration, clone, Git/non-Git switch or missing path blocks
attachment; V1 has no automatic identity migration. HEAD/branch drift can be
acknowledged on the same verified Workspace by the trusted host. A bounded
`RepositoryEvidence` or equal HEAD/status cannot prove file contents unchanged:
**all restored tool observations are historical**, even on an apparently equal
checkout. Refresh repository context and memory freshness before every new
generation as today; reverify changes before any later publication.

An unresolved attempt always requires manual reconciliation. The host reports
its base checkpoint, times, fenced call references and uncertainty. The human
must inspect possible effects and surviving children/remote work before
acknowledging abandonment; a stored PID is not authority to kill a process.
Resolution records actor label, timestamp, fixed `abandon_without_replay`
disposition, safe bounded note and current binding. It does not assert that
effects were absent. Restore only the committed history, never manufacture tool
success/failure messages for crashed calls. Supply a host-built recovery notice
as request context on subsequent turns while abandoned attempts exist; it
identifies the attempts and states that their effects were manually reconciled
and the old prompt/calls must not be replayed. Do not alter the saved conversation.

A new human-requested turn can intentionally authorize new work, but V1 cannot
prove that a newly generated command is semantically different from a prior
mutation. The guarantees are no automatic redispatch and fail-closed unresolved
attempts, not exactly-once tool effects.

## Exact checkpoint and side-effect boundaries

The write sequence is:

1. Enforce the runtime ownership/admission check below before trace creation,
   events, live state changes or store writes. Then under the lifetime writer
   lock and single-turn guard validate binding/input/limits, capture `turn_start`,
   allocate Turn ID and commit intent.
2. Use the existing Agent loop, fallback and admission. Accept response into
   live state only after complete-response assembly/validation; no durable step
   or response becomes a resume position.
3. For **every** tool, including apparently read-only tools: existence and
   argument validation → current permission decision → validate fence data →
   commit fence → `mark_tool_execution_started` → await `tool.execute`.
   Fence contains ordinal, assistant-message index, ToolCall ID and tool name,
   never arguments, permission grants or command output. Reject duplicate or
   non-monotone ordinals. A fence failure must escape as a session fault before
   execution, not become an ordinary model-facing tool failure to retry.
4. Preserve today's tool-message-before-event and pending-call rules. On abort,
   `_settle_aborted_turn` decides rollback versus preservation. Closed message
   groups prove correspondence, not whether failed/cancelled tools mutated.
5. After all owned model/tool work and cleanup settle, and after `trace.close`
   returns, obtain its immutable summary and validate/encode the full state.
   **Commit here, inside run_turn's finalization, before returning the response
   or re-raising its primary execution exception.** `agent_finish` and
   `trace_finish` do not acknowledge durability.
6. Atomically append checkpoint, finish the attempt and advance session head.
   Only a returned checkpoint receipt acknowledges durability. CLI may have
   streamed provisional text; after a commit error it must show that the turn
   was not durably acknowledged, block another input and retain live facts.

An ordinary sink failure already recorded by Tracer permits a checkpoint with
`trace_persisted=false`. Tracing disabled gives null trace fields. A raised trace
callback/finalization error follows existing rollback rules, blocks a durable
commit and leaves the intent for reconciliation. Trace JSONL and SessionStore
are independent; neither can repair the other.

On session-write failure retain live AgentState and the primary execution error;
add only a sanitized durability diagnostic to an existing error/cancellation.
If execution otherwise succeeded, raise `SessionDurabilityError` and gate further
turns. Never convert cancellation into success. Do not rollback executed facts
because SQLite failed. Asynchronous inspection/cleanup needed by finalization
must finish through existing `await_owned`; abandoned work uses `settle_owned`.
Use short synchronous SQLite transactions initially, avoiding an unobserved
background database write after cancellation. If a COMMIT acknowledgement is
uncertain, block and inspect the exact Turn ID/receipt; never retry run_turn.
A confirmed already-committed identical receipt is a metadata observation only.
An admission/fence/storage fault latches the recovery gate: normal finalization
must not clear it by attempting a later checkpoint, even if memory cleanup
finished and the store became available again. A fresh read/host reconciliation
is required.

## Persistence schema and API

New modules, relative to `src/cairn/`: `infra/sessions.py` (strict storage DTOs,
codec, concrete SQLite store, safe paths/local lock) and `runtime/session.py`
(concrete DurableSession binding and recovery gate). No new core port or
generic persistence framework is needed. Both modules have an actual caller in
the opt-in loop/host. Schema and checkpoint format versions are separate.

Proposed schema (foreign keys enabled; JSON is validated by Python, not trusted
because SQL accepts it):

~~~sql
PRAGMA user_version = 1;

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 0),
    head_seq INTEGER NOT NULL CHECK (head_seq >= 0),
    state TEXT NOT NULL CHECK
        (state IN ('ready', 'executing', 'recovery_required')),
    active_turn_id TEXT,
    binding_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK ((state = 'ready' AND active_turn_id IS NULL)
        OR (state = 'executing' AND active_turn_id IS NOT NULL)
        OR state = 'recovery_required'),
    FOREIGN KEY (session_id, head_seq)
        REFERENCES checkpoints(session_id, seq) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, active_turn_id)
        REFERENCES turns(session_id, turn_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE turns (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id TEXT NOT NULL,
    base_seq INTEGER NOT NULL,
    task_id TEXT,
    state TEXT NOT NULL CHECK (state IN
        ('executing', 'completed', 'interrupted', 'recovery_required', 'abandoned')),
    input_digest TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    ended_at_utc TEXT,
    settled_seq INTEGER,
    diagnostics_json TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_id),
    CHECK ((state IN ('completed', 'interrupted') AND settled_seq IS NOT NULL)
        OR (state IN ('executing', 'recovery_required', 'abandoned')
            AND settled_seq IS NULL)),
    FOREIGN KEY (session_id, base_seq)
        REFERENCES checkpoints(session_id, seq),
    FOREIGN KEY (session_id, settled_seq)
        REFERENCES checkpoints(session_id, seq) DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX one_unresolved_turn ON turns(session_id)
    WHERE state IN ('executing', 'recovery_required');

CREATE TABLE checkpoints (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    turn_id TEXT,
    format_version INTEGER NOT NULL CHECK (format_version = 1),
    created_at_utc TEXT NOT NULL,
    payload BLOB NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    PRIMARY KEY (session_id, seq),
    UNIQUE (session_id, turn_id),
    CHECK ((seq = 0 AND turn_id IS NULL) OR (seq > 0 AND turn_id IS NOT NULL)),
    FOREIGN KEY (session_id, turn_id)
        REFERENCES turns(session_id, turn_id) DEFERRABLE INITIALLY DEFERRED
);
~~~

`diagnostics_json` is one bounded typed record: fences in invocation order, fixed
failure/recovery codes, nullable trace reference, and optional resolution.
It has no resumable tool results. Terminal records/checkpoints are immutable;
only an unresolved attempt's diagnostics/status can advance. State/foreign-key
cross-checks also require a terminal turn's `settled_seq = base_seq + 1`, matching
checkpoint Turn ID, and head equal to the highest committed sequence. Initial
create inserts Session plus empty checkpoint in one deferred-constraint transaction.

Concrete host-only API (proposed signatures; no registered model tools):

~~~python
# Revision contains session_id, version, head_seq; receipts also contain turn_id.
class SessionStore:
    def __init__(
        self,
        directory: Path,
        *,
        mode: Literal["writer", "inspect"] = "writer",
        forbidden_roots: tuple[Path, ...],
        sensitive_values: tuple[str, ...] = (),
    ) -> None: ...
    def inspect(self, session_id: str) -> RecoveryReport: ...  # read-only
    def create(self, binding: SessionBinding) -> CheckpointReceipt: ...
    def begin_turn(self, expected: Revision, intent: TurnIntent) -> Revision: ...
    def fence_tool(self, expected: Revision, fence: ToolFence) -> Revision: ...
    def commit_turn(
        self, expected: Revision, settled: SettledTurn
    ) -> CheckpointReceipt: ...
    def require_recovery(self, expected: Revision, reason_code: str) -> Revision: ...
    def reconcile(self, expected: Revision, resolution: HostResolution) -> Revision: ...
    def close(self) -> None: ...


class DurableSession:
    @classmethod
    def create(
        cls, store: SessionStore, *, agent: Agent, binding: SessionBinding
    ) -> "DurableSession": ...
    @classmethod
    def reopen(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        agent: Agent,
        binding: SessionBinding,
    ) -> "DurableSession": ...
    @property
    def recovery_report(self) -> RecoveryReport: ...
    def reconcile(
        self, report: RecoveryReport, *, host_resolution: HostResolution
    ) -> None: ...
    def close(self) -> None: ...


# run_turn alone calls the bound session's begin/fence/settle helpers.
~~~

`reopen` loads/validates without running the Agent and returns a suspended session
bound to the supplied fresh Agent; its recovery report includes the Revision.
The host must confirm attachment/reconcile through `session.reconcile` before
it is runnable.
On a clean reopen this confirms current binding without abandoning any turn.
On an unfinished reopen it atomically abandons the exact unresolved attempt.
`require_recovery` may first mark that attempt recovery-required. This same
reconciliation operation handles acknowledged HEAD/provider drift; replaced
identity requires a separate explicitly created session, not a rebind.
The host constructs resolution objects from its UI/caller decision, never
model text. A Revision alone confers no OS lock, permission or host approval.
Create/reopen require an empty Agent with no active turn; reject an already
populated Agent instead of overwriting its history.

### Runtime-enforced Agent ownership

Use one Agent-local private `_durable_owner` slot (initially None), exposed through
a read-only `durable_owner` property and a once-only private binding method.
DurableSession.create/reopen installs its exact object identity before attachment
I/O or state restoration, with admission suspended until setup/reconciliation
finishes. Binding cannot be replaced. Failed attachment and `close` leave the
owner marker installed and non-runnable; neither operation silently downgrades
the Agent to stateless. Reopening uses a fresh Agent. No global registry or
serialized ownership token is needed; AgentState and checkpoint DTOs stay unchanged.

The first operation in `run_turn` checks the slot against the optional `session`:
both None allows today's stateless path; otherwise `session is agent.durable_owner`
and `session` must own this exact Agent. Require an open owner/store with its
lifetime lock and a ready, reconciled admission gate. Reject omitted/wrong/closed,
suspended/recovery-required or busy bindings with `SessionAdmissionError` before
any event, provider/tool/approval call, user-message append or durable mutation.
Check and reserve the single-turn guard synchronously before the first await;
passing a session cannot auto-bind an unbound Agent. The admitted turn still uses
the same `run_turn` implementation. Deliberate private-field/direct-SQL tampering
by arbitrary host code remains outside the threat boundary; ordinary bare-loop
calls are checked and rejected.

### Transaction and storage rules

Each mutation does bounded prevalidation, then `BEGIN IMMEDIATE`; rereads and
checks the expected revision/state/binding; applies the operation; CAS-updates
`sessions` with `WHERE session_id=? AND version=? AND head_seq=?` and checks one
affected row; commits. Increment `version` for **every** mutation including
fences, so a stale diagnostic writer cannot overwrite newer progress.

The terminal commit transaction inserts `checkpoint(seq=N+1)`, updates the
matching active attempt to completed/interrupted with `settled_seq=N+1`, then
CAS-updates head, state=ready and active_turn_id=NULL. Any failed CAS/constraint/
write rolls back **all** changes. Payload validation happens before BEGIN;
no provider, tool, prompt or Git command runs inside a transaction. Restore
reads head, checkpoint and attempt in one read transaction, validates digest and
relationships, then releases it. No `INSERT OR REPLACE` or last-writer-wins.

Use WAL and `synchronous=FULL`, `foreign_keys=ON`, `trusted_schema=OFF` and a
2,000 ms busy timeout; confirm settings and bounded database/page limits.
Inspect mode uses `mode=ro` and `query_only=ON`, never initializes a missing
database/schema, acquires no writer lock and rejects every mutation. SQLite may
need WAL sidecar access for a read-only snapshot; it is not a checkpoint writer.
Check schema, `quick_check`
and relevant foreign-key/row relationships before returning a report; unavailable
read-only WAL state fails explicitly rather than reopening with write access.
`BEGIN IMMEDIATE` obtains a write transaction before updates and can fail busy.
[SQLite transaction semantics](https://www.sqlite.org/lang_transaction.html).
WAL readers see consistent committed snapshots; a WAL flush to the main file is
distinct from this design's conversation checkpoint. WAL requires a local
filesystem. [SQLite WAL](https://www.sqlite.org/wal.html).
FULL sync protects committed WAL transactions under the documented filesystem
assumptions. [SQLite synchronization](https://www.sqlite.org/pragma.html#pragma_synchronous).

Keep one writable connection and one canonical host store for V1. Its writer
holds a nonblocking exclusive local OS file lock for the **entire** attached
session, not just SQL writes, and an in-process guard rejects overlapping turns
and duplicate admissions before any LLM/tool call. On macOS/Linux use
`fcntl.flock` on a stable private lock inode, with close-on-exec descriptors;
never unlink an apparently stale lock, expire a lease or steal it based on PID/
timestamp. Reopening acquires this lock before classifying old intents. Read-only
inspection cannot mark another live process interrupted. Unsupported lock/local
filesystem conditions fail closed. CAS is still required for stale handles.
The writer-mode store owns that lock until `close`; DurableSession cannot release
it while a bound turn or owned finalization is still active.

This intentionally admits one durable process per canonical store, even across
different Session IDs/Workspaces. Hosts must not run the same Workspace through
multiple independent stores; stateless processes/unrelated programs are outside
this lock. The lock does not undo already-issued effects or kill children after
a parent crash: an unfinished fence still blocks the next owner.

Reuse MemoryStore's exclusive DB creation and no-symlink checks for every path
component, DB/WAL/SHM/journal/lock file; private directories 0700 and files 0600.
Use a host-selected external state directory (CLI default
`~/.local/state/cairn/sessions`), outside **all** sandbox-writable roots, including
Workspace, effective TMPDIR/cache roots and caller-added tool roots. Recheck
descriptor/path identity before mutations and reject root changes. Parent
creation must be synchronized before acknowledging first durable create.
Private modes prevent other users, not arbitrary same-user host access or reads
by the current Bash sandbox. Do not promise transcript confidentiality.

Unknown schema/format, malformed data, missing head, hash mismatch or corruption
fail closed without deletion, silent fallback to an older checkpoint, automatic
reset or downgrade. Schema 1 creation accepts only an empty new DB or verified
schema 1. Future migrations require explicit host action, exclusive lock, a
SQLite-consistent backup and a reviewed transactional migration; do not copy the
main file alone while WAL is active. Retention/export/deletion are later work.
WAL maintenance is idle bounded housekeeping after a successful commit;
maintenance failure cannot retroactively report that committed turn uncommitted.
Do not copy MemoryStore's post-commit TRUNCATE/error handling as an acknowledgement
boundary.

Before shipping, validate the deployment SQLite runtime against current WAL
fixes (3.51.3+, 3.50.7 or 3.44.6 backports, or a documented equivalent).
[SQLite WAL-reset fix](https://www.sqlite.org/wal.html#walreset).
The audited project Python reports SQLite 3.50.4; this design does not upgrade
it. Single writable-connection/lock discipline constrains the race, but dependency
support remains an implementation gate.

## Failure and recovery matrix

All reopen outcomes below invoke zero model/tool/publisher calls. `C_N` means
the last committed full conversation, `I` an intent and `F` at least one fence.

| Failure window | Durable evidence | Possible external effect | Reopen / recovery action |
| --- | --- | --- | --- |
| Before intent commits | `C_N`; no I | No admitted model/tool | Restore C_N after explicit host attachment. |
| Intent committed; before/during model request or partial stream, before complete reply | `C_N + I`, possibly earlier F | Provider request/billing and earlier tool effects may have happened | Restore C_N; discard partial assembly and surface unfinished attempt; abandon by host reconciliation. Never resend prompt automatically. |
| Complete validated reply accepted/appended; before first tool fence | `C_N + I`, no F | No tool through this boundary | Same; response is not a checkpoint. Do not infer a resume position from trace text. |
| Preflight/approval failure or denial | `I`, no new F | That call cannot execute | Live loop follows existing failure rules; settled commit is possible. If process dies first, reconcile I. Approval is not persisted. |
| Fence COMMIT fails or acknowledgement unknown | `I`, F absent or present | This invocation must not start | Block live session; inspect exact revision. Crash recovery is conservative even if F committed just before death. |
| Fence committed; before tool entry | `C_N + I + F` | Possibly none; indistinguishable on restart | Recovery-required; human reconciliation. Do not use F as a retry ticket. |
| During tool, including partial mutation or tool raising after write | `C_N + I + F` | Partial/full mutation, surviving child or remote work possible | Restore C_N; retain F and files; stop/inspect possible work through trusted host procedures. No automatic kill, rollback or replay. |
| Tool returned; before tool message or after message but before following model | `C_N + I + F` | Effect may be complete | Same; in-memory result/trace cannot promote a checkpoint. |
| Abort cleanup or trace callback interrupted | `C_N + I`, possibly F | Earlier effects retained; outcome uncertain | Recovery-required. Closed-looking message groups are insufficient settlement evidence. |
| Fully settled; before checkpoint transaction | `C_N + I`, possibly F | Effects completed/failed as observed live | Recover C_N only; losing a final response does not authorize rerunning the turn. |
| During checkpoint transaction | Either complete `C_N/I` or complete `C_(N+1)/terminal attempt` | Same effects in either case | Assert atomic old/new state and relationships; no partial head or half terminal status. Old case requires reconciliation. |
| COMMIT durable; before receipt/turn_committed delivery/CLI output/process exit | `C_(N+1)` with terminal attempt | Already observed | Restore C_(N+1); show committed outcome. Discover by Turn ID, never resubmit the prompt because output was lost. |
| Trace sink failed, checkpoint succeeded | `C_(N+1)`, trace_persisted=false | Independent of tracing | Restore normally; missing trace is a diagnostic, not lost conversation or retry authority. |
| Secrets/size/DB failure after effects | Prior C plus I/F, no rejected payload | Effects may exist | Preserve live facts, gate session and sanitize error; no transcript dump or silent stateless continuation. |
| Reopen with changed Workspace/provider | Valid checkpoint, mismatched current binding | Old history may be stale; new endpoint not approved | Inspect only. Reject identity replacement; require fresh provider approval/HEAD acknowledgement and empty grants before a new turn. |
| Corrupt head/record or unknown schema | Untrusted/incomplete storage | Cannot establish outcome | Refuse execution; retain DB and report fixed error. Do not scan back to an older head and continue. |

## Integration diff plan

| Current method / boundary | Proposed change | Preserved contract |
| --- | --- | --- |
| `runtime.loop.run_turn(agent, user_input, *, budget)` | Add optional `session: DurableSession \| None = None`. First enforce the Agent-local owner/gate; then begin, build fence callback and settle/commit after abort handling and `trace.close`. Notify `turn_committed` after its receipt outside execution/rollback handlers. | `-> str` and primary execution exceptions; same loop, budgets, rollback and pending ownership. Both Agent owner and argument None preserve stateless behavior with no storage I/O. |
| `runtime.tool_execution.execute_tool_call` | Add optional `before_execute: Callable[[], None]` passed by the loop with its host ordinal/call reference. Invoke outside the execution-exception catch after approval, before mark/execute. | Preflight precedes permission; capability-derived ToolExecutionContext; tool result, cancellation and event ordering. No SessionStore import or lifecycle decisions here. |
| `runtime.turn_trace.TurnTrace.close` | Expose a typed immutable `summary` after finalization, including nullable usage and a sanitized persistence flag/code. | One final trace event; no second telemetry finalizer. `Tracer`/TraceStore continue independently. |
| `runtime.agent.Agent`, `runtime.assembly.build_agent` | Keep constructor/build signatures; add Agent-local private owner slot, read-only property and once-only binding hook using the reviewed M1-B lifecycle contract. DurableSession factories bind before restoration; closing retains the marker. | No credentials/IDs/owner reference added to AgentState or persistence. Unbound callers/evals stay stateless; bound Agents cannot bypass admission by omitting session. |
| `runtime.provider_runtime.ProviderSession.commit` and selection | No API change. Host observes committed runtime when constructing/updating the next Turn's binding. | Build replacement before rebinding; per-candidate matching admission. Persisted references cannot switch a provider. |
| `terminal.cli.root/main` | Later slice adds explicit create/reopen flags, store setup, read-only recovery display and trusted human resolution; pass optional session to run_turn. Default invocation bypasses all of it. | Existing input/model commands; commands only between turns. Fresh permission handler on reopen, current provider consent before credentials/network. Durable failure blocks another turn. |
| `runtime.tasks.CodingTaskRunner._execute / _turn` | No M1-C Task execution change. M2 creates a separate durable Session for each fresh Task Agent and passes it through _turn. Persist final TaskResult only after owned inspection/cleanup. | `run() -> TaskResult`, TaskStatus meanings, fresh Agent per Task and external cancellation propagation. |
| `WorktreeHandle`, LocalWorkflow/review/publisher | No V1 changes. Read public ownership facts when a future Task integration needs diagnostic provenance. | Existing retained Worktree and publication guards; no automatic handle reconstruction, checkout, index reset, release or PR retry. |

A recovered session's host-built recovery notice is optional system request
context in the existing `run_turn` request construction, never conversation
messages or a second resume loop. It is bounded and included before admission
with all other system-message overhead for every fallback candidate.
M1-A/M1-B own LLMClient streaming and Event API changes; M1-C consumes their
approved fully assembled response boundary. This persistence subdesign preserves
ModelExecutor's `(request, response)` completion contract, ToolResult, permission
policy, ContextBuilder trimming, Project Memory, RepoGraph and frozen Eval artifacts.

## Staged implementation and acceptance

These are **M1-C slices**, gated by M1-A Streaming/M1-B Session Lifecycle milestone
acceptance and independent review of this design; Store-first implementation is
not proposed. No provider runs or remote publication are needed. Each slice gets
focused tests and semantic diff review; full offline checks run at integration.
Under `tests/`, proposed new store tests belong in `infra/sessions/`, runtime
tests in `runtime/` and CLI tests in `terminal/`. Shared fault helpers belong in
the existing `tests/support/`.

| Slice | Deliverable | Independent observable acceptance |
| --- | --- | --- |
| M1-C.1 — storage/codec contract | Strict checkpoint/attempt DTOs, concrete SessionStore, version/schema rules, atomic create/begin/fence/commit/reconcile, safe storage and lifetime lock. **No loop/CLI change.** | Real temporary SQLite DB; full-state roundtrip, invalid data rejected, CAS/atomicity/crash/lock/path/secret tests below. |
| M1-C.2 — opt-in turn boundary | DurableSession, enforced Agent binding/admission and tool fence hooks; typed TurnTrace summary; recovery gate/context and post-receipt lifecycle acknowledgement. | Stateless suite unchanged; deterministic stream/LLM/tool doubles cover ownership rejection, settled aborts, fencing, acknowledgement failure and no redispatch. |
| M1-C.3 — explicit local create/reopen UI | CLI flags and recovery inspection/host acknowledgement; provider approval and grants reset. | Public CliRunner/pipe input, no real provider. Opening/reconciling performs zero model/tool calls; absent opt-in creates no store. |
| M2 — separate Task recovery milestone | Versioned Task metadata/results linked to Session/Turn; retain current runner as owner. | Crash after committed turn/before inspection/result shows an unfinished Task without rerunning it; temporary Git repos test provenance and retention. |
| Later — separately designed idempotency | Only when a concrete tool has a verified effect/receipt protocol | Independent design/version/tests; never infer it from checkpoint sequence or trace success. |

Deterministic tests must assert final observable state, not incidental SQL call
counts. Parameterize the failure-matrix windows using host fault hooks/barriers,
scripted LLMs and a local file-mutating tool double:

| Test family | Exact oracle |
| --- | --- |
| Streaming/acknowledgement | Script interleaved text/call fragments, parseable-but-unfinished arguments, an incomplete call group and fallback attempt changes: no partial assistant/tool group enters AgentState and no partial tool executes. A complete validated response is accepted once and uses the existing fenced tool path. Cancel before completion, both before any tool and after an earlier accepted tool: discard the buffer, retain only settled existing facts. Tokens/finish/trace events acknowledge no checkpoint; turn_committed follows a receipt with matching IDs/seq/outcome. Commit failure emits no acknowledgement; failed acknowledgement delivery preserves the committed receipt and primary outcome/cancellation without replay. |
| Agent ownership | Snapshot live state and all session/turn/checkpoint rows/revisions after fixture setup. Call public run_turn on a bound Agent with session omitted/None (also after close or failed attachment), a different owner object (even with the same Session ID), a correct but closed owner/store, or a suspended/recovery-required/busy gate. Each rejects before events, approval, model/tool calls or live/durable mutation. Rebinding and passing an owner to an unbound/different Agent also reject. Controls: matching ready owner takes the durable path; owner=None/session=None retains normal stateless behavior. |
| Codec/history | Roundtrip Unicode/nulls/empty rolled-back turn and interrupted closed groups. Reject extra/duplicate JSON keys, bad digest/version, orphan/duplicate/out-of-order results, duplicate IDs inside a group and changed committed prefix. Reused IDs in different groups succeed. Full history survives request trimming. |
| Atomicity/CAS | A stale Revision cannot begin/fence/commit/reconcile; stale reconciliation cannot clear a newer attempt. Inject failure after checkpoint INSERT and after attempt UPDATE: head/attempt/checkpoint remain old together. Duplicate terminal commit cannot append another checkpoint. |
| Process crash | Parent waits for a child readiness signal at each named window, kills/exits child, then reopens actual SQLite. Accept precisely old or new transaction state with consistent head/terminal links; never partial rows. Tool double increments a file and fsyncs it before its crash barrier: after reopen its count remains 1 and calls remain 0. |
| Concurrent writers | Child A holds lifetime lock while paused inside a tool; child B's writable reopen/create fails busy before any LLM/tool call, even for another Session ID. Read-only inspection succeeds without classifying A interrupted. After A dies B acquires lock but its unresolved attempt gates execution. In-process overlapping turns also fail before effects. |
| Live failures | Inject failure before execution, mutation-then-exception, later LLM/permission error, budget exhaustion and result-event failure. Existing rollback/preserved tool facts survive; any committed checkpoint has closed groups and the correct interrupted outcome. A secret-like returned result refuses commit without deleting live facts. |
| Finalization/cancellation | Sink failure plus successful commit preserves response and false trace flag; callback failure prevents commit. Cancel LLM/tool and repeat cancellation during owned cleanup/finalization; cleanup finishes, primary CancelledError propagates, state is committed as settled interruption or remains gated with intent. Late cancellation never causes a second run. |
| Corruption/storage limits | Unknown schema, missing head, mismatched IDs/seq, oversize record/DB/WAL and damaged JSON fail explicitly with files retained. Disk-full/I/O/COMMIT-ack fault blocks continuation; rejected payload never appears in DB/WAL, session diagnostics or fallback reports. WAL-maintenance failure after commit preserves its receipt. |
| Authority/bindings | Reopen cannot reuse a session NETWORK grant; new ASK without human approval cannot execute. Malformed calls fail preflight before approval and before fence. Changed provider endpoint requires current consent; invalid config cannot reuse stored references. Replaced root, clone, registration or unavailable Git context blocks attachment. |
| Workspace/history | Temporary Git repositories preserve HEAD/index/source files across storage tests. Changed HEAD requires acknowledgement. Editing file contents with unchanged HEAD/status never makes historical tool observations current evidence. Worktree provenance cannot call retain/release/adopt or authorize publication. |
| Path/security | Reject empty forbidden-root list and overlap with Workspace/TMPDIR/uv cache/additional roots, symlink components/DB/sidecars/lock and replaced descriptors. Normal file/Bash tools cannot modify the external store. Known secrets and encoded credential-like fields fail before transaction with fixed safe errors. |
| Task milestone | Cancel/crash during post-turn repository inspection retains committed Session and unfinished Task separately; no synthesized TaskResult/verdict. External cancellation remains observable. Verification/publication requires fresh exact-tree evidence and authorization. |

SIGKILL/process-exit tests establish application-crash behavior, not a power-loss
test. OS/power-loss expectations rely on SQLite FULL sync and working local
filesystem durability primitives; document that limit. Use existing native
sandbox readiness helpers for actual Bash isolation and report skips explicitly.

## M2 Task recovery without scheduling

In the separate M2 design, a schema migration can add a `tasks` row keyed by
Session ID and existing Task ID, with version, input digest, phase, nullable
TaskResult and public Worktree provenance. The **runner** records `running → turn_committed →
inspecting → result_recorded` around its current operations. A committed turn is
not a completed Task: repository inspection can still fail or be cancelled.
On restart an unfinished Task becomes recovery-required, leaving the Session
checkpoint intact. An explicitly authorized, read-only local inspection may be
repeated only after binding/ownership validation; an old Agent turn is never
rerun to obtain a missing TaskResult.

Worktree adoption needs its own checked protocol; serialized paths, state labels
or report files cannot reconstruct WorktreeHandle's private ownership proof.
Existing workflow recovery reports and publication-attempt claims remain
diagnostics/guards. Missing verification/PR receipt requires reconciliation and
fresh evidence, not republishing. Future scheduling calls CodingTaskRunner and
references these IDs/revisions; this schema does not require a queue, DAG,
dispatcher, background worker or distributed lock.

## Implementation gates and deferred decisions

M1-C starts only after M1-A/M1-B milestone acceptance and independent review
of this design, including the ownership and streaming negative-test oracles above.
Release/CLI gates remain:

- Reject sensitive/oversize checkpoints instead of redacting conversation.
  Confirm proposed bounds and false-positive handling before the CLI slice.
- Establish a supported SQLite deployment/runtime version and platform lock
  support before shipping; observed SQLite 3.50.4 needs evaluation against the
  documented WAL fixes. No dependency/runtime changes are part of this design.

CLI flag spelling, retention/export UX, Worktree adoption and Task-result schema
are deferred; Task recovery belongs to M2. The next implementation work is
**M1-A Streaming, then M1-B Session Lifecycle**. After those prerequisites and
review, the first persistence slice is **M1-C.1: isolated SessionStore and codec**
with deterministic SQLite crash/CAS/lock/security tests. This branch remains
design-only.
