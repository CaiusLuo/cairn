# Single-node pilot runbook

This is the admission gate and the failure drill for one user-managed,
single-machine, single-concurrency Cairn pilot. It decides whether a host may
start a pilot and how an operator recovers one interrupted by a crash.

It deliberately does not describe queues, distributed workers, containers,
high availability or unattended 24/7 service. Nothing here qualifies a SaaS
deployment. See [Out of scope](#out-of-scope).

Run every command from the repository root of the Cairn checkout you intend to
pilot, with the locked development dependencies installed
(`uv sync --locked --all-groups`).

## Profiles and status

Two profiles exist, and they have different gates. Check only the profile you
are starting.

| Profile | Contents | Extra gate |
| --- | --- | --- |
| `local-stateless` | Interactive CLI, tools, permissions, traces. No persistence. | Preflight only |
| `local-durable-pilot` | The above plus `cairn session new/reopen/reconcile` and the durable Task ledger | Preflight **and** a fixed-SQLite runtime ([#45](https://github.com/CaiusLuo/cairn/issues/45), [#46](https://github.com/CaiusLuo/cairn/issues/46)) |

Recorded status for the macOS development host this runbook was verified on
(macOS 26.3.1 arm64, APFS) is in [Status on the recording host](#status-on-the-recording-host).
That section is evidence for that host and runtime only.

## Preflight

Preflight is offline: it calls no provider, opens no network connection and
requires no API credential. It reports one `PASS`, `SKIP` or `BLOCKED` line per
check and exits non-zero when the selected profile is blocked.

```sh
uv run python examples/pilot/preflight.py
uv run python examples/pilot/preflight.py --profile local-durable-pilot
```

`SKIP` means the check does not apply to this profile; it is not a pass. For
example, `sqlite-wal-admission` is `SKIP` under `local-stateless` because the
stateless CLI constructs no `SessionStore` and never reaches the admission gate.

Options:

| Option | Purpose |
| --- | --- |
| `--profile local-stateless` | Default. Required checks exclude SQLite admission. |
| `--profile local-durable-pilot` | Adds the SQLite admission gate and store-open check. |
| `--state-dir DIR` | Probe a different state directory than `~/.local/state/cairn`. |
| `--allow-unverified-sqlite` | Report a declared-unverified runtime for development. It still reports `BLOCKED` for the durable profile. |

The checks are:

| Check | What it establishes |
| --- | --- |
| `python-version` | CPython is at least 3.12 and the exact interpreter is printed |
| `sqlite-wal-admission` | `sqlite_wal_fix_supported()` and `wal_admission()` for the linked SQLite, with the required versions |
| `bash-sandbox` | `sandbox-exec` (macOS) or `bwrap` (Linux) actually runs a command **and** an outside write is refused |
| `sandbox-read-boundary` | States the real read boundary: write-jailed on both platforms, read isolation only on Linux |
| `cli-import` | The `cairn` package imports and `cairn --help` lists its commands |
| `state-directory` | The state directory exists and is writable |
| `state-directory-permissions` | The observed mode; world-readable is reported, not hidden |
| `state-directory-isolation` | The state directory is outside the current Workspace |
| `session-store-gate` | The real `SessionStore` refuses or admits exactly as the reported verdict says, and a refusal leaves no directory |
| `trace-output` | A `Tracer` with `JsonlTraceSink` writes one readable JSONL record |
| `provider-config` | `.env` or `.cairn/models.toml` validates structurally, without reading credentials |

The preflight reuses production code rather than reimplementing checks:
`sqlite_wal_fix_supported`/`wal_admission` and `SessionStore` from
[store.py](../../src/cairn/infra/sessions/store.py), `BashTool` for the sandbox,
`load_project_providers` for configuration, and `Tracer`/`JsonlTraceSink` for
traces. It adds no CLI subcommand.

### Reading the verdict

- **All required checks not `BLOCKED`** — the profile may start.
- **`sqlite-wal-admission` is `BLOCKED`** — start the stateless profile, or
  select an interpreter whose linked SQLite carries the reviewed WAL-reset fix.
  Do not set `CAIRN_ALLOW_UNVERIFIED_SQLITE=1` for a pilot: a declaration admits
  a store but is never a supported deployment.
- **`bash-sandbox` is `BLOCKED`** — do not run Bash-dependent pilot tasks. The
  sandbox fails closed; a host without one must not be treated as isolated.
- **`provider-config` is `BLOCKED`** — fix the configuration before starting.
  Invalid TOML never falls back to the environment silently.

### Clean-install smoke

The preflight covers import and `--help` against the working tree. For a wheel
install, use the documented packaging check in
[AGENTS.md](../../AGENTS.md#verification):

```bash
uv build
```

Then install the built wheel into a fresh virtual environment and confirm the
entry point:

```sh
uv run python -m venv /tmp/cairn-pilot-smoke
/tmp/cairn-pilot-smoke/bin/pip install dist/cairn-*.whl
/tmp/cairn-pilot-smoke/bin/cairn --help
```

A passing import is not a durable qualification: repeat the preflight inside the
new environment before trusting it.

## Failure drill: SIGKILL, inspect, confirm, reconcile

This drill is reproducible offline. It uses the same two-phase `run_turn`
mechanism, real SQLite and a real SIGKILL, and it is the regression that guards
the procedure. The provider is scripted, so no token is spent.

```sh
uv run pytest -q \
  tests/runtime/tasks/test_task_recovery.py::test_sigkill_recovery_preserves_git_and_receipts_without_turn_or_tool_replay
```

The remaining windows are covered by the surrounding recovery and store suites:

```sh
uv run pytest -q tests/runtime/tasks/test_task_recovery.py
uv run pytest -q tests/infra/sessions
```

The procedure the drill encodes has four steps, and no step may be skipped.

### 1. Inspect, read-only

```sh
uv run cairn session inspect SESSION_ID
uv run cairn session inspect SESSION_ID --store /path/to/store
```

Inspection opens an existing store read-only, creates no Agent, requests no
provider approval and performs no work. It reports the Session ID, last
committed checkpoint, recovery state, current revision, any unfinished Turn and
the effect-fence ordinals.

An effect fence means external effects may already have occurred. Inspect the
surviving Workspace and any external state before deciding anything.

### 2. Confirm the binding by hand

Compare the reported facts with reality before acknowledging:

- the Workspace, Git HEAD and branch are the ones you expect;
- the provider binding matches the configuration you intend to continue with;
- the unfinished Turn is the one you interrupted.

If any of these differ, stop. Do not reconcile a binding you cannot explain.

### 3. Reconcile explicitly — never automatically

```sh
uv run cairn session reconcile SESSION_ID
```

`reconcile` prints the exact token and demands it verbatim:

```text
abandon_without_replay SESSION_ID TURN_ID_OR_none REVISION_VERSION
```

Refusal, EOF and noninteractive input never approve. Changed Git HEAD/branch or a
changed provider binding is identified before the token is requested, and an
observation that changes during confirmation rejects the operation.

Abandonment records the resolution. It never replays the interrupted Turn, never
rolls back effects that already happened, and never manufactures a result.

### 4. Start new work from a clean boundary

```sh
uv run cairn session reopen SESSION_ID
```

`reopen` constructs a fresh Agent and permission handler; conversation messages
are restored, permission and network grants are not. Only then can new Turns be
admitted.

### Durable Tasks

An unfinished Task is not fixed by Session reconciliation. Read a fresh Task
report, construct the exact resolution from that report's revisions and input
digest, and pass it to `CodingTaskRunner.reconcile_task`. The decision table
(running without a committed Turn, committed-but-unfinished, inspecting, or
already recorded) is in [Durable Task Ledger](task-ledger.md#explicit-trusted-host-task-reconciliation).
Recovery performs no model call, no tool replay and no publication.

## Stop conditions

Stop the pilot and investigate; do not "try again" any of these:

| Condition | Action |
| --- | --- |
| A permission request was denied | The capability was not granted. Do not widen it to continue; restart with an explicit grant if the task truly needs it. |
| SQLite reports an unfixed or unknown version | Durable mode is `BLOCKED`. Continue in stateless mode or select a qualified interpreter. Never declare the runtime verified. |
| An effect fence or unknown external effect exists | Treat the external state as possibly changed. Inspect before reconciling; never replay. |
| A trace cannot be written | Persistence or trace uncertainty stops admission. Resolve storage before continuing. |
| You suspect a secret leaked | Stop. Rotate the credential, then inspect traces and Bash child environments. Configured credentials are stripped from Bash children, but files the user can read remain readable. |
| Git metadata protection is refused (`UnprotectedGitMetadata`) | The workspace layout is unsupported. Move to a supported checkout; do not work around the refusal. |

Interrupted work is never auto-resumed. There is no automatic model, Task, Turn,
inspection or mutation retry.

## Recording-host status

Measured on the macOS development host named below. This is evidence for that
host, interpreter and filesystem, not a platform or deployment claim.

| Fact | Value |
| --- | --- |
| Host | macOS 26.3.1 (build 25D771280a), arm64, local APFS |
| Interpreter used for preflight | CPython 3.12.13 (uv-managed project `.venv`) |
| Linked SQLite | 3.50.4 |
| `wal_admission()` | `refused` (no declaration set) |
| Sandbox | `/usr/bin/sandbox-exec`, writes outside the workspace refused |

### Per-mode status

| Mode | Status | Basis |
| --- | --- | --- |
| **Stateless** | **PASS** | Preflight `local-stateless` reported no blocking check on this host. Sandbox enforcement, `cairn --help`, writable state directory and JSONL trace output were each verified. |
| **Durable** | **BLOCKED** | The uv-managed interpreter links SQLite 3.50.4; `wal_admission()` is `refused`, so `SessionStore` refuses and every durable command fails closed. This host is not qualified for durable mode. |
| **GitHub Publishing** | **BLOCKED** | Not exercised. It requires the separately authorized live smoke in [#50](https://github.com/CaiusLuo/cairn/issues/50); no real Draft PR has been created or verified by this runbook. |

### What is not claimed

- The Homebrew CPython 3.12.15 / SQLite 3.54.0 combination is recorded as
  **PASS (host-qualified)** for durable admission in the
  [SQLite runtime support matrix](../performance/sqlite-runtime-support.md).
  That verdict belongs to that explicitly selected interpreter on this host. It
  does **not** qualify the uv-managed interpreters linking SQLite 3.50.4, the
  default project `.venv`, the platform, or any other filesystem.
- The qualification report was obtained at main `6e87a558...` on that
  interpreter. This runbook did not re-run it, and the dedicated qualification
  environment from that report no longer exists on this host, so no durable
  claim is made here.
- SIGKILL evidence is application-crash evidence, **not** power-loss durability.
  Deployment filesystem and device-synchronization behavior remains unvalidated.
- Read isolation is a Linux property. On macOS a sandboxed command can read any
  host file the invoking user can read; write isolation is the guarantee there.
- There is no CPU, memory or wall-clock quota beyond the tool timeout.

## Related

- [SQLite runtime support](../performance/sqlite-runtime-support.md) — measured
  runtimes, admission and the release verdict.
- [Durable CLI](durable-cli.md) — approval, inspection and reconciliation
  semantics.
- [Durable Task Ledger](task-ledger.md) — Task phases and explicit recovery.
- [Native Bash sandbox](sandbox.md) — the enforced boundary and its limits.
- [Configuration](../configuration.md) — provider and credential layouts.
- [Offline coding task E2E](offline-coding-e2e.md) — the offline acceptance path
  for one coding task.
- [Task summaries](task-metrics.md) — bounded per-task metrics and missing-data
  semantics.
