# Session checkpoint performance

The benchmark in [session_checkpoints.py](../../evaluation/benchmarks/session_checkpoints.py)
uses deterministic four-message Turns, one fenced read reference per Turn, 1,024
ASCII bytes in each tool result and final assistant message, and real SQLite
WAL/FULL transactions. It never executes the tool or contacts a provider. Run:

```sh
uv run python -m evaluation.benchmarks.session_checkpoints --output /tmp/checkpoints.json
```

## Measurement method

Each size uses seven unprofiled samples. Inspect is measured at exactly 5, 10, 20,
40 or 80 Turns. Steady writes are the next seven distinct Turns, so the window is
N+1 through N+7, without mutation replay. Tool-free commits use the following
N+8 through N+14 window (user plus 1,024-byte assistant), identically before and
after.

The profiled fenced write follows those windows. SQL COMMIT timings are separate
from full commit admission, encoding and idle WAL maintenance. WAL peak is
sampled immediately after SQLite COMMIT and before idle maintenance; idle WAL
size is reported separately.

cProfile diagnostics are separate inclusive measurements; nested columns must
not be added together. `tracemalloc` peak captures a separate inspection's
Python allocations, excluding SQLite native allocations and OS caches.

Cold reopen constructs a fresh inspector and SQLite connection with the writer
closed and WAL absent; it does not flush the operating-system page cache. Live
reopen measures fresh readers while the writer is idle. Neither read path writes
source files.

The reference timings supplied with the task (5.2/15.0/49.5/200.6/748.4 ms at
`945776b`) are historical reported observations, not this benchmark's results.

## Architecture choice

A, removing redundant V1 passes, can reduce constants but still reads and checks
the sum of full historical prefixes and retains quadratic storage growth.
B, incremental V2 records, stores and checks one exact tail per Turn with the
validated predecessor receipt. This is the selected design. C, trusted cached
validation, would need an additional invalidation and trust contract for external
SQLite changes; it is unnecessary here and is not implemented.

V2 uses one bounded history accumulator and one final materialization. All
historical tails still undergo semantic validation; schema quick-check, FK and
payload-size scans remain. No previous validation result survives a read epoch.
A nonunique Turn Session index supplies insertion order through SQLite's implicit
rowid suffix; EXPLAIN verifies that V2 traversal uses no temporary sort.
Full runtime snapshot validation and prefix comparison remain at commit admission,
and `_prepare` / `_current` retain independent checks across their transactions.
Thus inspection and each ordinary write are linear in current stored history;
a sequence of N writes still performs O(N²) cumulative validation work. The goal
is to remove O(N²) work from each inspection/write and quadratic stored prefixes,
not to promise constant-time commits.

## Compatibility and limits

See the [schema/format matrix](../guides/session-store.md#codec-and-transactions)
and [explicit migration](../guides/task-ledger.md#schema-compatibility-and-explicit-upgrade).
Legacy V1 prefixes remain unchanged, including their hashes and Task attribution.
A mixed chain pays the full legacy cost plus linear V2 cost; an upgrade does not
rewrite old prefixes to manufacture a faster legacy benchmark. Read-only recovery
never migrates. The final host view remains `CheckpointPayload`; the receipt hashes
the stored delta, not a re-encoding of the materialized view.

All records and the logical full snapshot remain bounded. Unkeyed SHA-256 links
detect corruption, not malicious whole-database rewriting by the trusted host.
The existing SQLite 3.50.4 WAL-reset deployment blocker and target filesystem
qualification requirements remain; these development results are not a release
or power-loss durability claim.

## Measurements

Measured on macOS 26.3.1 arm64, Python 3.12.13, Pydantic 2.13.5 and
SQLite 3.50.4. Both implementations used the same interpreter and locked
dependencies. Baseline source was a clean `git archive` of `945776b`, imported
through `PYTHONPATH`; V2 used `30559d6`. Runs were sequential after the tests
finished. All latency cells below are median [minimum, maximum] in milliseconds,
with seven samples, unless stated otherwise.

Raw evidence: [baseline](session-checkpoints-baseline.json),
[V2](session-checkpoints-v2.json).

| Turns | V1 inspect | V2 inspect | V1 cold reopen | V2 cold reopen |
| --- | --- | --- | --- | --- |
| 5 | 8.03 [7.80, 8.26] | 5.21 [5.10, 5.23] | 8.86 [8.64, 9.23] | 6.38 [6.32, 6.55] |
| 10 | 25.69 [24.23, 28.28] | 9.53 [9.31, 9.87] | 26.13 [25.70, 27.09] | 10.81 [10.69, 10.91] |
| 20 | 104.03 [100.91, 112.88] | 18.63 [18.26, 18.91] | 102.80 [101.39, 108.87] | 20.06 [19.52, 20.46] |
| 40 | 335.63 [335.37, 338.95] | 35.96 [35.50, 36.14] | 340.41 [337.75, 357.11] | 36.82 [36.77, 37.33] |
| 80 | 1306.89 [1300.31, 1327.62] | 71.04 [70.61, 73.81] | 1309.25 [1304.56, 1312.41] | 72.84 [72.49, 76.11] |

At 80 Turns, V2 inspection is **5.44%** of the baseline, below the 25% target.
V2 inspection grows 13.64× for 16× as many Turns;
checkpoint payload bytes grow 15.51×.

| Turns | V1 ordinary commit | V2 ordinary commit | V1 fence | V2 fence | V1 fenced commit | V2 fenced commit |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 124.42 [89.87, 162.87] | 63.72 [56.23, 72.23] | 33.82 [16.66, 59.05] | 16.55 [11.35, 22.12] | 49.59 [26.30, 79.71] | 40.96 [27.97, 53.76] |
| 10 | 207.42 [159.68, 257.27] | 82.51 [77.55, 89.10] | 79.50 [51.55, 117.69] | 24.99 [19.93, 30.36] | 103.11 [70.36, 146.01] | 61.29 [48.07, 73.89] |
| 20 | 460.40 [358.92, 506.96] | 124.59 [117.51, 132.72] | 256.90 [206.27, 297.20] | 42.68 [37.91, 48.00] | 291.48 [242.91, 337.81] | 102.81 [89.90, 114.16] |
| 40 | 1143.94 [1025.86, 1249.05] | 211.02 [204.05, 214.52] | 772.32 [674.50, 920.19] | 80.80 [72.36, 84.35] | 847.86 [740.87, 981.28] | 188.56 [170.95, 199.88] |
| 80 | 3633.35 [3228.85, 3957.62] | 377.99 [369.34, 391.75] | 2798.08 [2606.31, 2999.35] | 151.31 [145.58, 158.27] | 2929.00 [2736.07, 3157.85] | 359.77 [342.26, 367.05] |

| Turns | Payload bytes V1 / V2 | DB bytes V1 / V2 | Peak WAL bytes V1 / V2 | Inspect Python peak bytes V1 / V2 |
| --- | --- | --- | --- | --- |
| 5 | 39,997 / 17,221 | 90,112 / 73,728 | 90,672 / 74,192 | 145,814 / 167,429 |
| 10 | 139,438 / 33,862 | 204,800 / 102,400 | 103,032 / 74,192 | 289,652 / 314,515 |
| 20 | 518,478 / 67,162 | 602,112 / 151,552 | 127,752 / 74,192 | 582,502 / 611,354 |
| 40 | 1,997,189 / 133,799 | 2,121,728 / 249,856 | 185,432 / 74,192 | 1,152,189 / 1,207,131 |
| 80 | 7,837,029 / 267,119 | 8,056,832 / 462,848 | 288,432 / 78,312 | 2,302,293 / 2,400,313 |

Peak Python materialization memory at 80 Turns increases from 2.30 MB to
2.40 MB (about 4.3%); V2 improves traversal and storage, not this allocation
peak. Both remain bounded and grow with the logical full history.

Idle WAL was zero in every before/after sample; the table shows transient
COMMIT peaks, including the write windows. Full begin/snapshot/SQL-COMMIT
measurements and live-reopen variations are retained in the raw evidence.

At 80 Turns, additional storage-path timings are:

| Operation | V1 | V2 |
| --- | --- | --- |
| Fenced begin | 1399.54 [1308.87, 1497.17] | 75.61 [73.46, 78.69] |
| Fenced snapshot | 1.14 [1.09, 1.17] | 1.10 [1.09, 1.17] |
| SQLite COMMIT (21 samples) | 0.29 [0.14, 0.67] | 0.24 [0.09, 0.41] |
| Ordinary begin | 1771.96 [1538.78, 1901.49] | 80.54 [77.78, 84.68] |
| Ordinary snapshot | 1.22 [1.14, 1.31] | 1.21 [1.16, 1.29] |

### Profile attribution at 80 Turns

Inclusive profiler milliseconds are diagnostic, overlap, and are slower than the
unprofiled measurements. Each cell is calls / cumulative ms. A dash means that
function is absent on that path. The write is one complete fenced cycle after
the measurement windows.

| Function | V1 inspect | V2 inspect | V1 write | V2 write |
| --- | --- | --- | --- | --- |
| `codec.py:decode_checkpoint` | 81 / 1577.07 | — | 476 / 11268.75 | 1 / 44.15 |
| `codec.py:decode_storage_checkpoint` | — | 81 / 59.97 | — | 475 / 342.32 |
| `codec.py:encode_checkpoint` | — | — | 1 / 65.25 | 2 / 131.99 |
| `codec.py:encode_record` | — | 1 / 18.71 | 13 / 44.80 | 20 / 177.20 |
| `codec.py:encode_storage_checkpoint` | — | — | — | 1 / 87.04 |
| `codec.py:snapshot_messages` | — | — | 1 / 1.37 | 1 / 1.46 |
| `codec.py:validate_history` | 81 / 837.26 | 81 / 22.19 | 477 / 6019.40 | 478 / 194.62 |
| `store.py:_commit` | — | — | 3 / 1.06 | 3 / 0.56 |
| `store.py:_current` | — | — | 2 / 4480.85 | 2 / 246.47 |
| `store.py:_prepare` | — | — | 4 / 4676.10 | 4 / 243.04 |
| `store.py:_report` | 1 / 1612.76 | 1 / 105.47 | 5 / 11467.75 | 5 / 605.39 |
| `store.py:_schema` | 1 / 2.53 | 1 / 0.19 | 5 / 18.76 | 6 / 1.38 |
| `store.py:_settled_relationship` | 80 / 8.59 | 80 / 0.51 | 471 / 62.93 | 471 / 3.20 |

The baseline is dominated by repeated checkpoint decoding and history validation,
not SQLite COMMIT or schema checks. V2 retains all semantic passes and independent
transaction reports, but each report traverses tails instead of repeated prefixes.


### Legacy-only compatibility path

The new implementation with an unupgraded schema-2/V1 database was measured
separately: [raw legacy evidence](session-checkpoints-legacy.json). These results
are not included in V2 averages. The historical payload bytes are identical to
the baseline for each corresponding size.

| Turns | Inspect ms | Cold reopen ms | Ordinary commit ms | Payload bytes |
| --- | --- | --- | --- | --- |
| 5 | 7.89 [7.67, 8.04] | 9.22 [8.91, 9.42] | 139.52 [101.93, 179.75] | 39,997 |
| 80 | 1389.87 [1383.21, 1506.42] | 1428.45 [1390.19, 1545.03] | 3942.99 [3694.57, 4115.62] | 7,837,029 |

At 80 Turns, legacy inspection remains quadratic and is 1.06× the old baseline
median. Extra aggregate-bound accounting adds work; this is a disclosed legacy
cost, not an optimized path. Explicit upgrade preserves that existing V1 cost;
only subsequently appended V2 records avoid repeating the full prefix.

## Implementation and review scope

Baseline is `945776be3071c198c38f00ee189e9661d70b2d9e`. The reproducible
benchmark began in `8f12da3`; implementation and regressions are in `30559d6`.
Development continues directly on `main`. No remote publication was performed.

The storage changes are in `src/cairn/infra/sessions/` (`models.py`, `codec.py`, `store.py`,
`paths.py`, `ledger.py`).
The only runtime change is expected-receipt encoding in
`src/cairn/runtime/durable_session.py`; Turn ownership stays in `run_turn`.
Regression changes are `tests/infra/sessions/` (`test_incremental.py`, `test_migration.py`,
`test_security.py`),
`tests/support/sessions.py`, `tests/runtime/tasks/test_durable_runner.py`, and
`tests/terminal/test_durable_cli.py`. The benchmark and the SessionStore, Task Ledger
and durable-session guides describe the resulting contract.

Independent review should concentrate on migration rollback/FK restoration,
unchanged historical receipt attribution, and middle-chain corruption with a
recomputed local digest. Offline tests do not replace target-filesystem durability
qualification or exact-head Linux CI.

## Verification

- Full offline suite: `uv run pytest --cov=cairn --cov-report=term-missing`:
  2,532 passed, five warnings; coverage 91.96%. This ran on the byte-identical
  implementation/test tree before its integration into `main`.
- After integration, `uv run pytest tests/infra/sessions
  tests/runtime/test_durable_session.py tests/runtime/test_durable_session_recovery.py
  tests/runtime/tasks/test_durable_runner.py tests/terminal/test_durable_cli.py -q`:
  418 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, and
  `uv run mypy src tests`: passed (305 formatted files; 277 typed source files).
- `uv build`, wheel installation into an independent virtual environment,
  `cairn --help`, and imports of `CheckpointDelta`, `SessionStore` and
  `DurableSession`: passed; imports resolved to the installed wheel.
- Frozen V1/V2 evaluation source, datasets and tests are unchanged against the
  baseline; the full offline suite includes their digest checks.
- Documentation link/path tests: `uv run pytest tests/docs/test_documentation.py -q`
  67 passed.
- Exact-head Linux CI is unverified: this local change was not pushed. Existing
  remote success at the baseline does not validate these changes. No paid provider
  or live publication operation was run.
