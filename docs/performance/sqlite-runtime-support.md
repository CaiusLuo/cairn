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
| Linux deployment | CPython **3.12.15** (uv standalone) | **3.53.1** | Debian 12, kernel 6.1.0-43-amd64, x86_64, ext4 | **verified** | **PASS** |
| Linux system Python (same host) | CPython 3.11.2 / 3.12.13 (distro) | **3.40.1** | Debian 12, ext4 | **refused** | **BLOCKED** |

A row is evidence for the interpreter and host it names, not for the platform or
for every Linux deployment. The Debian host carries two runtimes with opposite
verdicts, which is the point: the verdict follows the linked SQLite, not the OS.

### Linux deployment host (verified)

Measured 2026-10-16 on Debian GNU/Linux 12 (bookworm), kernel 6.1.0-43-amd64,
x86_64, ext4 (`rw,relatime,errors=remount-ro`), 4 vCPU.

```
$ uv run python -c "import sqlite3, sys; print(sys.version, sqlite3.sqlite_version)"
3.12.15 (main, ...) 3.53.1
```

`sqlite_wal_fix_supported()` returns `True` and `wal_admission()` returns
`verified`, so no declaration is involved. The system `python3` and the
pre-existing project venv on the same host link SQLite 3.40.1 and stay refused;
they were left untouched. The verified runtime is an independently installed
uv-managed CPython in its own virtual environment — installing it changed no
global interpreter or library.

Observed lock configuration on that host: `journal_mode=wal`,
`locking_mode=normal`, `synchronous=2` (FULL).

### macOS development host

The interpreter statically links its own SQLite, so the `sqlite3` CLI on PATH is
irrelevant to Python: replacing the CLI does not change the runtime. No
uv-managed CPython on this host reports a SQLite version carrying the fix, and
`pysqlite3-binary` publishes no macOS arm64 wheel, so a fixed runtime is not
available here by installing a package.

### Release verdict

Durable execution is **not** a supported deployment on the macOS development host.
The blocker there is the linked SQLite 3.50.4, not the code.

The Debian host is **PASS** on the interpreter recorded above. That verdict covers
that interpreter, filesystem and locking configuration only. It does not qualify
every Linux host, other filesystems, container storage drivers or network
filesystems, and it does not qualify the system interpreters on the same machine.

The Debian suites were run against the verified runtime without any admission
declaration; `CAIRN_ALLOW_UNVERIFIED_SQLITE` was unset throughout.

### Available upgrade path

1. Run on a runtime whose SQLite reports a verified version, for example a Linux
   deployment image built with SQLite 3.51.3 or newer.
2. Or build CPython against a verified SQLite; the uv-managed standalone builds
   link their own copy and do not follow the system library.
3. Re-run the durable suites on that runtime and record its versions here.

Upgrading the global Python or SQLite is out of scope and must not happen
silently.

## Evidence

macOS development host, where the suites need the unverified declaration:

```bash
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
uv run pytest -q tests/infra/sessions/test_security.py -k unverified
uv run pytest -q tests/infra/sessions tests/runtime/tasks tests/terminal
```

Debian host, on the verified runtime and with no declaration set:

```bash
uv run python -c "import sqlite3, sys; print(sys.version); print(sqlite3.sqlite_version)"
uv run pytest -q tests/infra/sessions tests/runtime/tasks tests/terminal
uv run pytest -q tests/infra/sessions/test_security.py tests/infra/sessions/test_process.py
```

The suites ran on the Debian host without the unverified declaration, because the
runtime is verified there; the macOS host still needs the declaration, and that
declaration is not production configuration. What those tests do establish, and
what they do not, is recorded in
[session store](../guides/session-store.md): real SQLite with spawned process
barriers, and SIGKILL as application-crash evidence, never as power-loss
durability.
