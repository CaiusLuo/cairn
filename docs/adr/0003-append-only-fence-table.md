# 3. Append-only tool-fence table (SessionStore schema V4)

Status: proposed

## Context

`SessionStore` stores one Turn per row in `turns`, with its whole
`AttemptDiagnostics` in a single `diagnostics_json` column. That column holds the
Turn's message start, binding snapshot, and **every** tool fence the Turn has
admitted.

A fence is not a log line. It is the durable evidence that a tool may have run,
and `fence_tool` must return a committed revision before the runtime is allowed to
execute the tool. The current write therefore rewrites the entire column:

```python
diagnostics = record.diagnostics.model_copy(
    update={"fences": (*record.diagnostics.fences, fence)}
)
diagnostic_raw = self._encode(diagnostics)  # whole record, whole history
```

Measured on the Linux host, with `MAX_RECORD_BYTES = 65_536` and
`MAX_INVOCATIONS = 256`:

| fences in the Turn | `diagnostics_json` bytes | one `encode_record` |
| --- | --- | --- |
| 64 | 14,938 | 4.7 ms |
| 128 | 29,652 | 9.2 ms |
| 256 | 59,220 | 19.6 ms |

Appending fence N re-encodes N-1 fences it already encoded, so a Turn costs
O(N²). Measured end-to-end, 256 fences take ~6.9 s, split as `_prepare` 5.86 s
(which decodes the growing record) and `_encode` 2.46 s; the write commit is
0.60 s and `_schema`'s integrity checks are 0.12 s (1.7%). The quadratic term is
the storage shape, not the integrity checks.

The append shape this record proposes was measured against SQLite with the same
WAL and `synchronous=FULL` settings, one INSERT plus one revision update per
fence inside its own transaction: 64 fences 0.115 s (1.80 ms/fence), 128 fences
0.236 s (1.84 ms/fence), 256 fences 0.370 s (1.44 ms/fence). Cost per fence is
flat, and the 256-fence case is roughly 18x faster than today's 6.9 s.

This record proposes schema V4: fences move to an append-only child table, so one
fence costs one INSERT regardless of how many came before.

## Decisions

### D1. `tool_fences` is the fence of record; `turns` keeps everything else

```sql
CREATE TABLE tool_fences (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 256),
    assistant_message_index INTEGER NOT NULL CHECK(assistant_message_index >= 0),
    call_id TEXT NOT NULL CHECK(length(call_id) BETWEEN 1 AND 256),
    tool_name TEXT NOT NULL CHECK(length(tool_name) BETWEEN 1 AND 256),
    observed_at_utc TEXT NOT NULL,

    PRIMARY KEY (session_id, turn_id, ordinal),
    UNIQUE (session_id, turn_id, assistant_message_index, call_id),
    FOREIGN KEY (session_id, turn_id) REFERENCES turns(session_id, turn_id)
);
```

`turns.diagnostics_json` keeps `binding`, `message_start`, `reason`, `resolution`
and `commit_version`, and stops writing `fences` (`AttemptDiagnostics.fences`
already defaults to `()`, so a V4 row decodes and the store reassembles the tuple
from the child rows on read). `AttemptDiagnostics` stays the in-memory shape
`RecoveryReport` exposes; nothing in `core` learns about `tool_fences`.

The `UNIQUE (session_id, turn_id, assistant_message_index, call_id)` constraint is
the DB-level twin of `_validate_record`'s `references` set, and the
`ordinal BETWEEN 1 AND 256` check is the twin of `max_length=MAX_INVOCATIONS`.
Both live in the table so they hold even if a future writer bypasses the model
validator. The call/tool-name length checks mirror `reference_text`; the no-control-
character half of `reference_text` cannot be expressed as a plain CHECK and stays
in the codec, enforced when the `ToolFence` model is built. A writer that bypasses
the model and INSERTs directly is not covered by that check — the table's CHECKs
and the read path's decode still reject the row on inspection, so the failure is
fail-closed rather than silent. So does the cumulative fence budget: the JSON
record's `MAX_RECORD_BYTES` no longer bounds a Turn's fences, and only
`MAX_DATABASE_BYTES` does. That is the intended trade, stated here so it is not
mistaken for an oversight.

### D2. Append is one transaction: INSERT, then CAS

`fence_tool` becomes:

```text
_prepare_fence(expected, turn_id)      # read session row + last fence for the turn
BEGIN IMMEDIATE
  _row(conn, expected)                 # revision still what _prepare saw
  read last ordinal / last
    (assistant_message_index, observed_at_utc)
  check the new fence follows it and is not a duplicate
  INSERT INTO tool_fences (...)
  _cas(...)                            # version + 1, head_seq unchanged
COMMIT
```

The INSERT and the revision CAS are in the same transaction. A crash before
COMMIT leaves neither; a crash after COMMIT leaves both. `commit_uncertain`
continues to route into the existing recovery path — a fence that may or may not
have committed is never retried as if it had not.

The pre-INSERT checks are the ones that can be answered from the session row and
the last fence: revision valid, turn `executing` and locally owned, ordinal
contiguous, message index and timestamp monotonic, `(index, call_id)` not already
present. The whole-history revalidation `_validate_record` performs is not needed
on the append path, because committed fences are immutable and were validated
when they were inserted. It stays on the read path (`_report`) where a report is
built from disk.

### D3. Credential and size checks become per-fence

Today the whole record is re-screened on every append by `encode_record`
(`_sensitive`, `_CREDENTIAL_FIELD`, `check_sensitive`, `MAX_RECORD_BYTES`). V4
screens the **one** new fence with the same functions, and bounds the row by the
column CHECKs above. That keeps the threat model — every fence passes a content
check before it is stored — while removing the O(N) rescan. `MAX_RECORD_BYTES`
continues to bound `diagnostics_json`; it no longer has to also bound N fences.

### D4. Migration is explicit, offline and verified

`upgrade_schema` gains a 3 → 4 step; V1/V2 stay readable under their existing
branches. The step runs only with no live turns and the lifetime writer lock, and
it:

1. Captures the V3 acceptance baseline: `inspect()` of every Session before the
   change (revision, state, attempts, fences, receipt).
2. Backs the database up exactly as the existing 1/2 → 3 step does.
3. Creates `tool_fences`, then copies every fence out of each
   `diagnostics_json`, preserving `(session_id, turn_id, ordinal)`.
4. Rewrites each `diagnostics_json` without `fences`, re-encodes it under
   `MAX_RECORD_BYTES`, and re-checks every migrated fence's identity, ordering,
   duplicate key and sensitive content.
5. Rebuilds `RecoveryReport` for every Session and compares the logical content
   with step 1; a mismatch aborts before COMMIT.
6. Commits `PRAGMA user_version=4` in one transaction. A failure leaves the V3
   database untouched and recoverable; no mixed state is admitted.

A read-only inspector keeps reading V3 by its own branch, so an un-migrated
database is inspected, not silently rewritten.

### D5. What this record does not decide

- Whether `tool_fences` also replaces the diagnostics' `fences` for
  `TaskLedgerMixin` (it currently shares `AttemptDiagnostics`). Out of scope until
  a Task owns fences.
- Whether fences should be pruned or archived for long Sessions. V4 makes them
  rows, so that becomes possible; it is not required to remove the quadratic.
- The exact SQL for `_schema`'s expected-DDL comparison. Adding a table changes
  the exact-string set, so every version branch (`v1`, `v2`, `v3`, `v4`) must
  list its own tables; the V4 branch is `(*_SCHEMA_V4, *_TASK_SCHEMA)`.

## Consequences

- A fence append touches one row instead of one growing column: costs stop
  depending on N. The 256-fence test should fall from seconds to well under one.
- `RecoveryReport` assembly now joins `turns` to `tool_fences` per Turn. Reads
  stay O(total fences), which they already were.
- The exact-schema check becomes four version branches instead of three; a
  mismatch for a hand-edited database still fails closed.
- The migration is the risky part, not the runtime path: it must be exercised
  against a real V3 fixture (crash mid-fence, `effects_unknown`, duplicate ordinal,
  abandoned turn) before this record moves from proposed to accepted.
