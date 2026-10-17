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
| macOS development (uv runtime) | CPython 3.12.13 (uv standalone) | **3.50.4** | macOS 26.3.1 arm64 | **refused** | **BLOCKED** |
| macOS development (Homebrew runtime) | **CPython 3.12.15** (Homebrew) | **3.54.0** | macOS 26.3.1 arm64, local APFS | **verified** | **PASS** (host-qualified) |
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

### macOS development host (qualified 2026-10-17)

**PASS only for the explicitly selected Homebrew CPython 3.12.15 interpreter**
on macOS 26.3.1 (build 25D771280a), arm64, local APFS, with Homebrew SQLite
3.54.0. The qualification report was obtained on this host at main
`6e87a558524b41b295ac8331579b03357f890826`.
`/opt/homebrew/bin/python3.12` (or its dedicated virtual environment) reports
`sqlite3.sqlite_version == "3.54.0"`, `wal_admission() == "verified"` without
`CAIRN_ALLOW_UNVERIFIED_SQLITE`. `otool -L` on its arm64 `_sqlite3` module
resolves `/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib` to Homebrew's
`sqlite/3.54.0` keg. The standard-library wrapper supports
`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`, `Connection.setconfig` and
`Connection.getconfig`, including set/read-back.

A separate live-WAL probe used `os._exit(0)` to preserve a committed WAL.
Read-only `vfs=unix-none` inspection read the committed row while leaving
DB/WAL/SHM bytes, identities, size, mode and modification times unchanged.
The host reported **791 passed, 0 failed, 0 skipped** across
`tests/infra/sessions` (294), `tests/runtime/tasks` (157),
`tests/terminal` (265) and three additional runtime Durable/recovery test
files (75). These are host-run results, not a macOS GitHub Actions job; the
qualified filesystem and interpreter are part of the evidence.

The pre-existing **uv-managed** CPython 3.12.13/3.13.12/3.14.3/3.15.0a7
on this host statically link SQLite 3.50.4 and remain **BLOCKED**. Replacing
the `sqlite3` CLI or using whichever `python3.12` happens to precede
Homebrew on PATH does not qualify those interpreters. `pysqlite3==0.6.1`
bundles a fixed SQLite 3.53.4 but lacks the required connection configuration
interface, so it is not a drop-in alternative. No SessionStore driver change or
third-party SQLite package was needed.

Homebrew upgrades, changes to the linked SQLite library, OS/filesystem
changes or interpreter replacement require requalification. Do not silently
change system Python or a project's default `.venv`.

### Release verdict

The macOS Homebrew interpreter and the Debian uv-managed interpreter listed
above are **PASS** for the tested host/runtime/filesystem combination only;
other macOS interpreters on the same machine are still **BLOCKED**. No verdict
qualifies every macOS/Linux host, every filesystem, network filesystems,
container storage drivers or power-loss durability. Both PASS suites were run
without an unverified declaration; `CAIRN_ALLOW_UNVERIFIED_SQLITE` was unset.

### Available upgrade path

1. On this qualified macOS host, explicitly use Homebrew CPython 3.12.15
   (`/opt/homebrew/bin/python3.12`) and its dedicated virtual environment;
   do not rely on an unqualified PATH default.
2. Elsewhere, use a CPython runtime actually linked to a fixed SQLite (for
   example the qualified Debian runtime), keeping the stdlib connection API.
3. Re-run durable suites, read-only inspection and crash recovery on each new
   host/runtime/filesystem and record its actual library linkage.

Upgrading the global Python or SQLite is out of scope and must not happen
silently.

## Evidence

macOS Homebrew runtime, using an isolated virtual environment from the
repository root (these commands do not modify the existing project `.venv`):

```bash
ROOT="$(mktemp -d /private/tmp/cairn-sqlite-verify.XXXXXX)"
/opt/homebrew/bin/python3.12 -m venv "$ROOT/venv"
env -u CAIRN_ALLOW_UNVERIFIED_SQLITE \
  UV_PROJECT_ENVIRONMENT="$ROOT/venv" UV_LINK_MODE=copy \
  uv sync --locked --all-groups --python /opt/homebrew/bin/python3.12
env -u CAIRN_ALLOW_UNVERIFIED_SQLITE "$ROOT/venv/bin/python" -c \
  'import sqlite3, sys; print(sys.executable, sqlite3.sqlite_version)'
env -u CAIRN_ALLOW_UNVERIFIED_SQLITE "$ROOT/venv/bin/python" -c \
  'from cairn.infra.sessions.store import wal_admission; print(wal_admission())'
env -u CAIRN_ALLOW_UNVERIFIED_SQLITE "$ROOT/venv/bin/python" -m pytest -q \
  tests/infra/sessions tests/runtime/tasks tests/terminal \
  tests/runtime/test_durable_session.py \
  tests/runtime/test_durable_session_recovery.py \
  tests/runtime/test_loop_recovery.py
```

The qualified host reported 791 passing tests for these suites and a separate
orphan-WAL inspection probe. The above commands provide a reproduction route,
not a claim that a fresh host has been qualified.

Debian host, on the verified runtime and with no declaration set:

```bash
uv run python -c "import sqlite3, sys; print(sys.version); print(sqlite3.sqlite_version)"
uv run pytest -q tests/infra/sessions tests/runtime/tasks tests/terminal
uv run pytest -q tests/infra/sessions/test_security.py tests/infra/sessions/test_process.py
```

The qualified Debian and Homebrew macOS runtimes need no unverified declaration;
unqualified uv-managed macOS runtimes remain refused. What these tests do
establish, and what they do not, is recorded in
[session store](../guides/session-store.md): real SQLite with spawned process
barriers, and SIGKILL as application-crash evidence, never as power-loss
durability.
