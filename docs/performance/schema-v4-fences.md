# Session schema V4 fence performance

Schema V4 moves each admitted `ToolFence` from `turns.diagnostics_json` into an
append-only `tool_fences` table. The design and its migration contract are
[ADR 0003](../adr/0003-append-only-fence-table.md); this page holds the measured
evidence.

## Method

Both sides run the same script, `scripts/session_store_stage_timing.py`, through
the production path (`SessionStore.fence_tool`), on the same Linux host with the
same SQLite build, filesystem and device. V3 is a worktree at `5936435`; V4 is
the schema above. Each row is the median of five runs over 16, 32, 64, 128 and
256 fences.

The stage intervals nest: `total` contains `write` (the transaction), which
contains `commit`; `prepare`, `codec` and `validate` also run inside the append.
They are reported side by side and must not be summed.

## Result

| fences | V3 total | V4 total | speedup | V3 ms/fence | V4 ms/fence | V3 `prepare` |
| --- | --- | --- | --- | --- | --- | --- |
| 16 | 0.125 s | 0.073 s | 1.7x | 7.81 | 4.54 | 0.066 s |
| 32 | 0.304 s | 0.145 s | 2.1x | 9.50 | 4.53 | 0.176 s |
| 64 | 0.754 s | 0.295 s | 2.6x | 11.78 | 4.61 | 0.507 s |
| 128 | 2.148 s | 0.598 s | 3.6x | 16.78 | 4.67 | 1.649 s |
| 256 | 7.101 s | 1.207 s | 5.9x | 27.74 | 4.71 | 5.911 s |

V3's per-fence cost rises with the number of prior fences (7.81 → 27.74 ms) and
its `prepare` term grows from 0.066 s to 5.911 s, which is the quadratic term.
V4's per-fence cost is flat (4.54 → 4.71 ms) and `prepare` is zero on the append
path, because the turn is read without decoding its fences.

The raw stage breakdown, including variance, is in
[`v3-v4-stage-timing.json`](schema-v4/v3-v4-stage-timing.json). The logical-state baseline
captured before the change is in
[`v3-baseline.json`](schema-v4/v3-baseline.json); `scripts/session_store_baseline.py`
reproduces both.

## What this does not claim

Absolute constant time. Each fence still takes one SQLite transaction with
`synchronous=FULL`, so the commit is a real fsync and the wall time depends on
the device. The claim is only that cost per fence no longer grows with the number
of fences already in the Turn.
