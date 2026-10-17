# 2. Storage ownership and persistence tiers

Status: accepted

## Context

Cairn now opens three unrelated stores, and each arrived with its own durability
rules:

- `src/cairn/infra/sessions/store.py` plus `src/cairn/infra/sessions/ledger.py`
  — `SessionStore` (conversation checkpoints) and `TaskLedgerMixin` (Task facts).
- `src/cairn/infra/memory/store.py` — `MemoryStore` (human-approved project
  knowledge).
- `src/cairn/infra/observability/storage.py` — `TraceStore` (one JSONL file per
  trace).

[ADR 0001](0001-modular-architecture-baseline.md) fixed where code lives and who
owns a Turn. It did not say which of these stores may claim to have made a fact
durable, nor what a checkpoint, an observation and an approval each promise after
a crash. [architecture.md](../architecture.md#where-mutable-state-lives) lists the
in-process state owners but says nothing about persistence.

This record states the ownership boundary a future change should not silently
reverse. It deliberately defines no storage framework, no new port and no second
database: the SQLite Durable design and the human recovery path stay as they are.
It is short; the commit history and the linked guides carry the detail.

Several facts below were established by reading the source for this record, and
one of them is a defect (`D3`). Nothing here is a claim that the current code has
been changed.

## Decisions

### D1. Three tiers, and a tier is a promise about a crash, not a place

Three categories, deliberately distinct, and none of them is "the place a byte
happens to live":

| Category | Promise after process death | What it owns |
| --- | --- | --- |
| **Telemetry / observations** | none | Live progress, span timing, diagnostics. Best-effort: a lost record is lost evidence, never a lost fact that a caller may rely on to resume. |
| **Journal** | Its **own owner** commits a record; that owner's recovery may replay or abandon it | An independent, lightweight, committed conversation or session log. It may persist confirmed conversation history **without** a SQLite receipt. Its guarantees are strictly weaker than Strict Durable: no external-effect fencing, no cross-store transaction, no approval chain. |
| **Strict Durable** | A fact exists only if that tier's **own owner-specific commit and evidence contract** says so | Conversation, Task lifecycle and approved claims under the current SQLite design, each committed by its owner. |

A tier is assigned to a *fact*, not to a module, and only if that fact's owner
satisfies the tier's contract. The same module may hold facts in two tiers only
when each is labelled.

**Strict Durable is not synonymous with "carries a `SessionStore`
`CheckpointReceipt`."** Each owner commits through its own mechanism and evidence
contract. `MemoryStore` is the counter-example already in the tree: it commits an
approved memory through its own SQLite transaction and CAS plus a human
attestation, and never obtains a `SessionStore` receipt. A durable fact is
therefore one whose *owning store* can prove the commit; requiring one particular
store's receipt to exist would exclude a tier member that is already shipping.

**Telemetry is not a Session Journal.** `TraceStore` writes spans on a
best-effort basis. A `trace_id` retained inside an approval is durable
*metadata* — a pointer — and is neither durable retention of the trace content
nor proof that the content still exists once traces are pruned. Treating
telemetry as recoverable session log would overstate what a crashed run can
resume.

Consequence: "it is on disk" never upgrades a tier. Only that tier's own
admission gate, transaction and owner-specific commit evidence do.

### D2. Each store owns its facts; no store owns another's lifecycle

| Component | Fact it owns today | Tier | Authority it does *not* have |
| --- | --- | --- | --- |
| `SessionStore` (`src/cairn/infra/sessions/store.py`) | Session rows, checkpoint history, CAS revisions, its own writer lock | Strict Durable | no live Turn decision, no permission, no dispatch |
| `TaskLedgerMixin` (`src/cairn/infra/sessions/ledger.py`) | independently revised Task records phase `running`/`turn_committed`/`inspecting`/`result_recorded` | Strict Durable | no dispatch, no recovery authority |
| `MemoryStore` (`src/cairn/infra/memory/store.py`) | approved claims, tombstones, version CAS | Strict Durable | no approval authority (that is `MemoryService` + human callback) |
| `TraceStore` (`src/cairn/infra/observability/storage.py`) | JSONL spans, the only persistent trace record | **Telemetry / observations** — best-effort, *not* a Journal | never a substitute for conversation or Task facts, and never a resume source |
| Live Turn facts (`runtime.turn_trace`, `core.state.AgentState`) | conversation, spans, usage | Ephemeral until a checkpoint commits | — |

Ownership is expressed in the API, following ADR 0001 `D3`. `MemoryStore`'s
mutators `serialize_entry`, `insert_entry` and `update_entries` are documented as
host-only, with every lifecycle transition owned by
`cairn.runtime.memory.service.MemoryService`
(`src/cairn/infra/memory/store.py` lines 180–244). `TaskLedgerMixin` never opens a
connection; it is mixed into `SessionStore` and relies on that class's locking and
transaction helpers (`src/cairn/infra/sessions/ledger.py` lines 1–7).
`TraceStore` owns only the trace directory (`storage.py` line 36).

Consequence: a future Journal implementation may read and retain what a Durable
owner committed, and may keep its **own** committed record of conversation, but
must never advance a Task phase or a Session revision by itself. Owning a fact
means owning the commit that makes it true; it does not mean another store's
receipt must exist first.

### D3. A persistence tier is claimed only after the runtime safety gate

`SessionStore.__init__` decides admission before it touches any path, lock or
database (`src/cairn/infra/sessions/store.py` lines 221–229). `wal_admission`
(lines 189–204) returns `verified`, `unverified` or `refused`, and `refused`
raises before construction proceeds. This is the only version judgement in the
repository: `sqlite_wal_fix_supported` (lines 158–165) admits SQLite 3.51.3+, and
the 3.50.7+ / 3.44.6+ backports. Declaring an unverified runtime admits a store
but never makes it supported (lines 168–183).

**Baseline defect that motivated this decision (pre-fix state).** The paragraphs
below describe `MemoryStore` **before** the remediation this ADR rules for. They
are the historical baseline that made the decision necessary, not a statement
about the current tree; the ruling itself is unchanged.

At that baseline, `MemoryStore._initialize` ran `PRAGMA journal_mode=WAL`
(`src/cairn/infra/memory/store.py` line 132) and `_safe_paths` already tolerated
the `-wal`, `-shm` and `-journal` sidecars (line 66), so it opened a WAL database.
It imported nothing from `cairn.infra.sessions` (imports at lines 9–26) and called
neither `sqlite_wal_fix_supported` nor `wal_admission`. A repository-wide search
for both names returned hits only in `src/cairn/infra/sessions/store.py`.

The gap was real on a real runtime, not hypothetical. In the authoring venv
(CPython 3.12.13, SQLite **3.50.4**) `wal_admission()` returned `refused` and
`sqlite_wal_fix_supported()` returned `False`, yet `MemoryStore` opened that same
runtime's WAL database without complaint. That interpreter is the macOS
development runtime already recorded as **BLOCKED** in
[sqlite-runtime-support.md](../performance/sqlite-runtime-support.md#recorded-environments).
`docs/guides/project-memory.md` lines 23–26 state that memory "uses WAL", so the
promise was already made in prose.

Three options were compared at the time of decision.

- **(a0) Extract a small neutral `infra` SQLite-policy helper.** The option the
  Owner accepted. Move `sqlite_wal_fix_supported` / `wal_admission` /
  `VERIFIED_SQLITE_VERSIONS` into a policy module under `infra` that both
  `sessions` and `memory` import, so the one version judgement stays one while
  `infra.memory` never imports `infra.sessions`. Migration cost: one move plus
  import rewrites; no schema, no data, no behaviour change. This is a helper, not
  a driver or backend framework — it holds a version rule and nothing else — and
  it does not by itself decide whether memory is refused.
- **(a) Share the SessionStore gate.** Reuse `wal_admission` and
  `sqlite_wal_fix_supported` rather than re-deriving a version check, which keeps
  "one version judgement" true. Migration cost: none for data; `journal_mode`
  stays WAL and the schema version does not change. Compatibility cost: memory
  users on an unverified runtime start failing where they previously worked, and
  `MemoryError` is currently a deliberately coarse contract, so a refusal needs a
  distinguishable, actionable message. It also introduces an
  `infra.memory` → `infra.sessions` import; the helpers are module-level with no
  store coupling, but that dependency direction needs an explicit decision.
  Choosing (a0) removed that objection, which is why the Owner ruled for it.
- **(b) Migrate `MemoryStore` to a rollback journal.** Measured in this task on
  SQLite 3.50.4: a database whose header is WAL reports `('wal',)` on open, and
  `PRAGMA journal_mode=DELETE` only succeeds when it owns the lock; with the WAL
  writer connection still open it raises `database is locked`, leaving the header
  as `wal` and the `-wal`/`-shm` sidecars in place for the next opener. A rollback
  journal also materializes `<db>-journal` during every write, which is a new
  window `_safe_paths` must reject. Cost: an in-place `journal_mode=DELETE`
  migration must run when no other connection is open and must be restartable
  under a live writer; it changes durability, concurrency and checkpoint
  behaviour that `_connect` relies on (`PRAGMA wal_checkpoint(TRUNCATE)` at line
  107 stops meaning anything), and leaves existing WAL databases needing that
  online conversion.

**Accepted: option (a0)**, ruled by the Owner on 2026-10-17. The policy moves to
a neutral `infra` module, so the `infra.memory` → `infra.sessions` dependency that
made option (a) weaker is moot. `MemoryStore` refuses an unverified runtime by
default, the Session development override is not extended to it, and existing
memory databases are left byte-for-byte intact. Option (b) is rejected because it
trades a gate for a schema-level migration plus a new file state, and because the
WAL-reset bug is a property of the runtime, not of the journal mode chosen per
database.

Implementation is #63 and is recorded as a separate change with regression tests.
The refusal message contract and the first Journal consumer remain unruled; per
[Decisions taken](#decisions-taken) they do not block #63.

### D4. The Journal tier is opened with its first consumer, not before

No component is a Journal today, and building one is a later, separate change.
`TraceStore` supplies evidence a Journal might read, but trace records are
best-effort and telemetry is not a Journal (`D1`): a trace sink failure is captured
by the tracer and a checkpoint may still commit with `persisted=False`
(`docs/guides/durable-session.md` lines 122–123). `TraceStore` also removes
derived `summaries/` sidecars as it encounters them (`storage.py` lines 197–212).

A future Journal may persist **confirmed conversation history on its own
authority**, without depending on a SQLite `Receipt`. It commits its own record
through its own owner-specific commit contract, exactly as each Strict Durable
owner commits through its own. What it must not do is borrow authority it does
not have: it may never advance a Task phase or a Session revision, and it may
never assert an external-effect or approval fact that only a Durable owner can
commit.

Its guarantees are strictly weaker than Strict Durable, and an entry says so: no
external-effect fencing, no cross-store transaction, no approval chain. A reader
that needs those guarantees must consult the owning Durable store.

Requiring every Journal entry to link to a SQLite receipt would make a
lightweight JSONL-only resume profile impossible and would silently commit the
tier to SQLite — the opposite of what this decision is for. The roadmap postponed
Journal *implementation*, not its independent semantics.

Telemetry is the weaker still category and is not Journal: a lost span is lost
evidence, not a recoverable conversation record.

Consequence: the tier exists as an ownership rule, not as a module. Adding an
in-memory or on-disk journal is a change that cites this decision; it does not
need a new interface, a registry or an event bus.

### D5. Strict Durable keeps one writer and human reconciliation

The existing Durable design is preserved unchanged: **one writer per Store**,
CAS revisions, a lifetime writer lock held over the store directory across
session IDs, and reopening suspended until trusted-host reconciliation
(`docs/guides/durable-session.md` lines 42–48, 112–132). The lock is scoped to the
store, not to a Session, so two Sessions in one store are **not** independently
writable. Task
recovery stays an explicit host decision against a fresh report
([Task Ledger](../guides/task-ledger.md#explicit-trusted-host-task-reconciliation));
stored records "never execute work or grant recovery/publication authority"
(`docs/architecture.md` lines 51–63).

Consequence: no storage change may add an automatic replay, a background
compactor, or a second process permitted to write the store. Human reconciliation
remains the only way back from a suspended state, and permission is never
restored by recovery. A recovery path that
needs to be faster is a change to the report, not to who may advance the
lifecycle.

## Rejected

- **A storage abstraction layer or repository pattern over the three stores.**
  They share a serialization format and nothing else; a port would be written
  before any caller needs to substitute one.
- **Moving `MemoryStore` into `SessionStore`'s database.** They have different
  identities, different lifetimes and different approval paths. Sharing a file
  would also force memory to inherit the Session writer lock.
- **Making `TraceStore` Strict Durable.** Traces are large, append-only and
  explicitly allowed to be lost; promoting them would promise durability the
  tracer does not provide.
- **A version check local to `MemoryStore`.** A second judgement of which SQLite
  versions are safe is how the two stores diverge again.
- **Calling a version mismatch "unsupported" only in documentation.** The repair
  for `D3` must fail closed, or the Strict Durable claim is prose.

## Consequences

- Every store has one owner and one tier; a new store must state both in review.
- The `D3` divergence is resolved by decision, not by this record: `MemoryStore`
  qualifies the runtime like the session store does, through one neutral policy
  module. The implementation is #63 with its own regression tests.
- Nothing in this record changes a schema, a migration step or the recovery API.
  `MemoryStore` schema version stays 1 and the Session store stays at its current
  version.
- Adopting this record is a documentation change. It authorises exactly one
  follow-up (#63) and no refactor, storage abstraction, shared database or
  Journal implementation.

## Decisions taken

The Owner accepted this ADR on 2026-10-17. Each item below is the ruling that
implementation must follow; none is still open.

1. **Remediation for `D3`: option (a0), a neutral `infra` SQLite-policy module.**
   `sqlite_wal_fix_supported`, `wal_admission` and `VERIFIED_SQLITE_VERSIONS` move
   to a policy module under `infra` that both `sessions` and `memory` import. One
   version judgement stays one, and `infra.memory` never imports
   `infra.sessions`. This is a policy holder, not a driver or backend framework:
   it answers a version question and nothing else.
2. **`MemoryStore` refuses an unverified runtime by default.** Project Memory
   stops being silently writable on a BLOCKED runtime. The refusal is the default,
   not an opt-in.
3. **The Session development override is not extended automatically.**
   `CAIRN_ALLOW_UNVERIFIED_SQLITE=1` continues to mean *Session store only*. It
   must not silently authorise approval storage. If memory ever needs a
   development override it requires its own explicit decision and its own
   declaration, so one store's development escape hatch can never widen into
   another store's data.
4. **Existing `MemoryStore` databases are left byte-for-byte intact.** No automatic
   `journal_mode` rewrite, no migration, no silent repair. On a refused runtime the
   data stays where it is and becomes unreadable to that host until a qualified
   interpreter is used. A future migration would be its own recorded change.
5. **A JSONL Journal is a separate, later implementation.** It is not built here,
   and nothing in this ADR's acceptance requires it. When it is built it owns its
   own committed conversation history with strictly weaker guarantees than Strict
   Durable, per `D4`, and it never depends on a SQLite `Receipt`.

Still unruled, and deliberately not blocking #63:

- **What a refusal may reveal.** A fixed `MemoryError` keeps today's coarse
  contract but is hard to act on; a distinct message names the runtime and version
  at the cost of widening an error contract hosts may pin.
- **Who the first Journal consumer is.** Naming one fixes its retention policy and
  its own commit contract (`D4`); naming none keeps the tier as a rule.
- **Whether an approval may cite a pruned trace.** Today an approval retains typed
  IDs whose trace may be pruned by `delete_oldest` (`storage.py` lines 80–115).
  Keeping it is honest and documented; strengthening it changes memory evidence
  requirements.

### Acceptance scope

This ADR decision authorises exactly one follow-up change: **#63, the SQLite WAL
safety-policy convergence for `MemoryStore`**. It does not authorise a project-wide
refactor, a storage abstraction, a shared database, a Journal implementation, or a
change to Session admission semantics.

## References

The runtime support evidence, the admission gate and the recorded hosts are in
[sqlite-runtime-support.md](../performance/sqlite-runtime-support.md). The
Session, Durable and Task contracts are in
[SessionStore](../guides/session-store.md),
[DurableSession](../guides/durable-session.md) and
[Task Ledger](../guides/task-ledger.md). Project Memory's storage and approval
rules are in [Project Memory](../guides/project-memory.md).
