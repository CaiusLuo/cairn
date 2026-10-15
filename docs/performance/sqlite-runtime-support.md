# SQLite runtime support matrix

Durable execution depends on SQLite's WAL. A runtime without the reviewed
WAL-reset fix can corrupt a database after a reset, so the store admits a
runtime only when that fix is present, or when the host declares an unverified
runtime for development.

`sqlite_wal_fix_supported()` in `src/cairn/infra/sessions/store.py` is the one
version judgement; nothing else re-checks a version.

## Versions carrying the fix

These are the only versions the store admits without a declaration. This table
is not a support claim for any host; the recorded hosts below carry the verdicts.

| Runtime | WAL-reset fix | Admission without a declaration |
| --- | --- | --- |
| SQLite 3.51.3 and later | included | admitted |
| SQLite 3.50.7 and later 3.50.x | backport | admitted |
| SQLite 3.44.6 and later 3.44.x | backport | admitted |
| any other version | not established | refused |

## Admission

```
              +-----------------------------------------------------+
              | SessionStore.__init__  (before any path, lock or DB) |
              +-----------------------------------------------------+
                                     |
                                     v
                        +----------------------------+
                        | fixed WAL-reset version?   |
                        +----------------------------+
                          |                        |
                         yes                       no
                          |                        |
                          v                        v
              +---------------------+   +-----------------------------+
              | admission=verified  |   | host declared unverified?   |
              | open normally       |   +-----------------------------+
              +---------------------+        |                 |
                                            yes                no
                                             |                 |
                                             v                 v
                              +----------------------+  +---------------------------+
                              | admission=unverified |  | refuse: SessionStoreError |
                              | open, never reported |  | sqlite_wal_unverified,    |
                              | as supported         |  | actionable message        |
                              +----------------------+  +---------------------------+
```

The refusal happens before the store touches the filesystem, so a refused
runtime leaves no directory, lock or database behind.

Unverified admission requires one explicit host declaration:

- the `CAIRN_ALLOW_UNVERIFIED_SQLITE=1` environment variable, or
- the `SessionStore(..., allow_unverified=True)` argument.

The argument accepts actual booleans only. Strings or other truthy objects are
invalid options; the environment declaration requires the exact string `1`.

Declaring admits the store; it never makes the runtime supported.
`SessionStore.wal_admission` records which of the two happened. It is a reported
fact for hosts and tests, not an enforcement point: nothing in the runtime reads
it, so a caller that presents an `unverified` store as supported would be wrong
rather than blocked.

Stateless callers are unaffected: the default CLI, the Pi TUI and every
EvalRunner or TaskRunner caller that opens no store never construct a
`SessionStore` and never reach this gate.

## Recorded environments

Measured per host. A row is evidence for that host only, never a deployment
claim.

| Host | Interpreter | Linked SQLite | Platform | Admission | Verdict |
| --- | --- | --- | --- | --- | --- |
| macOS development | CPython 3.12.13 (uv standalone) | **3.50.4** | macOS 26.3.1 arm64 | **refused** | **BLOCKED** |
| Linux deployment | not available | not measured | not measured | not exercised | **BLOCKED** |

The Linux row is unmeasured: this session has no Linux host, and the acceptance
requires an actual runtime version rather than a CI guess. It stays BLOCKED until
someone records a real `python -c "import sqlite3; print(sqlite3.sqlite_version)"`,
the filesystem and lock configuration, and the durable suites on that host.

### macOS development host

The interpreter statically links its own SQLite, so the `sqlite3` CLI on PATH is
irrelevant to Python: replacing the CLI does not change the runtime. No
uv-managed CPython on this host reports a SQLite version carrying the fix, and
`pysqlite3-binary` publishes no macOS arm64 wheel, so a fixed runtime is not
available here by installing a package.

### Release verdict: BLOCKED

Durable execution is **not** a supported deployment on this host. The blocker is
the linked SQLite 3.50.4, not the code. Release needs a verified runtime and the
target filesystem and locking qualification in
[session store](../guides/session-store.md#verification-and-release-gate).

### Available upgrade path

1. Run on a runtime whose SQLite reports a verified version, for example a Linux
   deployment image built with SQLite 3.51.3 or newer.
2. Or build CPython against a verified SQLite; the uv-managed standalone builds
   link their own copy and do not follow the system library.
3. Re-run the durable suites on that runtime and record its versions here.

Upgrading the global Python or SQLite is out of scope and must not happen
silently.

## Evidence

```bash
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
uv run pytest -q tests/infra/sessions/test_security.py -k unverified
uv run pytest -q tests/infra/sessions tests/runtime/tasks tests/terminal
```

The durable suites run on this host only because the test suite declares the
unverified runtime; that declaration is not production configuration. What those
tests do establish, and what they do not, is recorded in
[session store](../guides/session-store.md): real SQLite with spawned process
barriers, and SIGKILL as application-crash evidence, never as power-loss
durability.
