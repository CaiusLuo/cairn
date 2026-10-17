# 2. Storage ownership and persistence tiers

Status: proposed

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

| Tier | Promise after process death | What may be reported as true |
| --- | --- | --- |
| Ephemeral | none | progress, live output, in-flight execution |
| Journal | one owner may replay or abandon an observation; no conversation fact | observed inputs and outputs, with provenance |
| Strict Durable | a fact exists only if a committed receipt says so | conversation, Task lifecycle, approved claims |

A tier is assigned to a *fact*, not to a module. The same module may hold facts in
two tiers only if each is labelled: `TraceStore` holds Journal evidence, while the
trace IDs an approval retains are Strict Durable provenance (`D2`).

Consequence: "it is on disk" never upgrades a tier. Only an admission gate, a
transaction and a receipt do.

### D2. Each store owns its facts; no store owns another's lifecycle

| Component | Fact it owns today | Tier | Authority it does *not* have |
| --- | --- | --- | --- |
| `SessionStore` (`src/cairn/infra/sessions/store.py`) | Session rows, checkpoint history, CAS revisions, its own writer lock | Strict Durable | no live Turn decision, no permission, no dispatch |
| `TaskLedgerMixin` (`src/cairn/infra/sessions/ledger.py`) | independently revised Task records phase `running`/`turn_committed`/`inspecting`/`result_recorded` | Strict Durable | no dispatch, no recovery authority |
| `MemoryStore` (`src/cairn/infra/memory/store.py`) | approved claims, tombstones, version CAS | Strict Durable | no approval authority (that is `MemoryService` + human callback) |
| `TraceStore` (`src/cairn/infra/observability/storage.py`) | JSONL spans, the only persistent trace record | Journal | never a substitute for conversation or Task facts |
| Live Turn facts (`runtime.turn_trace`, `core.state.AgentState`) | conversation, spans, usage | Ephemeral until a checkpoint commits | — |

Ownership is expressed in the API, following ADR 0001 `D3`. `MemoryStore`'s
mutators `serialize_entry`, `insert_entry` and `update_entries` are documented as
host-only, with every lifecycle transition owned by
`cairn.runtime.memory.service.MemoryService`
(`src/cairn/infra/memory/store.py` lines 180–244). `TaskLedgerMixin` never opens a
connection; it is mixed into `SessionStore` and relies on that class's locking and
transaction helpers (`src/cairn/infra/sessions/ledger.py` lines 1–7).
`TraceStore` owns only the trace directory (`storage.py` line 36).

Consequence: a future Journal implementation may read and retain what the Durable
owner committed, but must never advance a Task phase or a Session revision by
itself.

### D3. A persistence tier is claimed only after the runtime safety gate

`SessionStore.__init__` decides admission before it touches any path, lock or
database (`src/cairn/infra/sessions/store.py` lines 221–229). `wal_admission`
(lines 189–204) returns `verified`, `unverified` or `refused`, and `refused`
raises before construction proceeds. This is the only version judgement in the
repository: `sqlite_wal_fix_supported` (lines 158–165) admits SQLite 3.51.3+, and
the 3.50.7+ / 3.44.6+ backports. Declaring an unverified runtime admits a store
but never makes it supported (lines 168–183).

**Recorded risk — `MemoryStore` claims Strict Durable without this gate.**
`MemoryStore._initialize` runs `PRAGMA journal_mode=WAL`
(`src/cairn/infra/memory/store.py` line 132) and `_safe_paths` already tolerates
the `-wal`, `-shm` and `-journal` sidecars (line 66), so it opens a WAL database.
It imports nothing from `cairn.infra.sessions` (imports at lines 9–26) and calls
neither `sqlite_wal_fix_supported` nor `wal_admission`. A repository-wide search
for both names returns hits only in `src/cairn/infra/sessions/store.py`.

The gap is real on a real runtime, not hypothetical. In this task's own venv
(CPython 3.12.13, SQLite **3.50.4**) `wal_admission()` returns `refused` and
`sqlite_wal_fix_supported()` returns `False`, yet `MemoryStore` opens that same
runtime's WAL database without complaint. That interpreter is the macOS
development runtime already recorded as **BLOCKED** in
[sqlite-runtime-support.md](../performance/sqlite-runtime-support.md#recorded-environments).
`docs/guides/project-memory.md` lines 23–26 state that memory "uses WAL", so the
promise is already made in prose.

Two remediations were compared.

- **(a) Share the SessionStore gate.** Reuse `wal_admission` and
  `sqlite_wal_fix_supported` rather than re-deriving a version check, which keeps
  "one version judgement" true. Migration cost: none for data; `journal_mode`
  stays WAL and the schema version does not change. Compatibility cost: memory
  users on an unverified runtime start failing where they previously worked, and
  `MemoryError` is currently a deliberately coarse contract, so a refusal needs a
  distinguishable, actionable message. It also introduces an
  `infra.memory` → `infra.sessions` import; the helpers are module-level with no
  store coupling, but that dependency direction needs an explicit decision.
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

**Recommendation: (a)**, applied as its own change with a regression test that
pins "memory refuses an unverified runtime unless declared" beside the existing
SessionStore admission tests. It removes the divergence at its source, costs no
data migration, and preserves the one version judgement. Option (b) is rejected
for now because it trades a gate for a schema-level migration plus a new file
state, and because the WAL-reset bug is a property of the runtime, not of the
journal mode chosen per database.

This decision stays **proposed**: the Owner has not accepted the added
`infra.memory` → `infra.sessions` dependency, the refusal message contract, or
the consequence for hosts currently on a blocked runtime (see the checklist).

### D4. The Journal tier is opened with its first consumer, not before

No component is a Journal today. `TraceStore` supplies the evidence a Journal
would retain, but trace records are best-effort: a trace sink failure is captured
by the tracer and a checkpoint may still commit with `persisted=False`
(`docs/guides/durable-session.md` lines 122–123). `TraceStore` also removes
derived `summaries/` sidecars as it encounters them (`storage.py` lines 197–212).

A future Journal entry may hold only what a Durable owner already committed,
plus observations those commits point at. Every entry must link to the receipt
that authorized it; it may never create a receipt.

Consequence: the tier exists as an ownership rule, not as a module. Adding an
in-memory or on-disk journal is a change that cites this decision; it does not
need a new interface, a registry or an event bus.

### D5. Strict Durable keeps one writer and human reconciliation

The existing Durable design is preserved unchanged: one writer Store per Session,
CAS revisions, a lifetime writer lock, and reopening suspended until trusted-host
reconciliation (`docs/guides/durable-session.md` lines 42–48, 112–132). Task
recovery stays an explicit host decision against a fresh report
([Task Ledger](../guides/task-ledger.md#explicit-trusted-host-task-reconciliation));
stored records "never execute work or grant recovery/publication authority"
(`docs/architecture.md` lines 51–63).

Consequence: no storage change may add an automatic replay, a background
compactor, or a second process permitted to write a Session. A recovery path that
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
- `D3` is a known defect with a recorded remedy rather than an undocumented
  divergence. Until it is fixed, `MemoryStore` may open a WAL database on a
  runtime that carries the WAL-reset bug.
- Nothing in this record changes a schema, a migration step or the recovery API.
  `MemoryStore` schema version stays 1 and the Session store stays at its current
  version.
- Adopting this record changes documentation only. The implementation change for
  `D3` (a) needs its own issue, regression test and a read-only-to-Durable
  re-check on each host.

## Decision checklist

Each item is an open question for the Owner. None is answered by this record.

1. **Accept (a) for `D3`, in full or deferred?** In full: memory stops working on
   blocked runtimes, matching Sessions. Deferred: memory keeps its current
   coverage but the Strict Durable claim stays untrue there.
2. **May `infra.memory` import `infra.sessions`?** Yes: one version judgement.
   No: the helpers need a shared, non-package home, which is a boundary change of
   its own.
3. **What may a refusal reveal?** A fixed `MemoryError` keeps today's coarse
   contract but is hard to act on. A distinct message names the runtime and
   version, at the cost of widening an error contract hosts may pin.
4. **Does declaring `CAIRN_ALLOW_UNVERIFIED_SQLITE=1` cover `MemoryStore`?** One
   variable for both stores is simpler to reason about; a memory-specific
   declaration keeps a dev Session from silently enabling approval storage.
5. **What happens to existing `MemoryStore` databases on a refused runtime?**
   Fail closed and require a qualified interpreter leaves data intact but
   unreadable to those hosts. Any automatic rewrite is out of scope and would
   itself need a migration.
6. **Who is a Journal consumer?** Naming one now fixes its retention and its link
   to receipts. Naming none keeps the tier as a rule (`D4`).
7. **May traces be referenced by an approval when the trace is gone?** Today an
   approval retains typed IDs whose trace may be pruned by `delete_oldest`
   (`storage.py` lines 80–115). Keeping it is honest and documented; strengthening
   it changes memory evidence requirements.

## References

The runtime support evidence, the admission gate and the recorded hosts are in
[sqlite-runtime-support.md](../performance/sqlite-runtime-support.md). The
Session, Durable and Task contracts are in
[SessionStore](../guides/session-store.md),
[DurableSession](../guides/durable-session.md) and
[Task Ledger](../guides/task-ledger.md). Project Memory's storage and approval
rules are in [Project Memory](../guides/project-memory.md).
