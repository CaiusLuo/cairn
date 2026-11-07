# Cairn Server

One `cairn` process owns Web/API, the LangGraph Runtime, the task queue, WeChat,
Schedule and personal memory. `cairn-cli` is a separate HTTP client. Client exit
or SSH detachment does not stop Server work. Production background ownership
belongs to systemd; foreground debugging uses the same bootstrap.

## Build and run

```sh
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run cairn --config /etc/cairn/config.toml
```

Copy [the example](../../deploy/config.example.toml) to a host-owned location
outside both workspace and worktrees. Edit workspace and data paths for the
installation. Real model startup requires the provider approval below. For an
explicit unpaid smoke test, remove `provider_approval` and set `offline = true`.
There is no automatic offline fallback.

```sh
uv run cairn --config /etc/cairn/config.toml check-config
# Run the client from the configured workspace:
uv run cairn-cli --config /etc/cairn/config.toml
uv run cairn-cli --config /etc/cairn/config.toml runtime list
```

Open `http://127.0.0.1:8777/chat.html`. The username is `cairn`; the password is
`data_root/runtime/runtime.token` (private mode 0600). Chat, Tasks, History and
Settings use the same listener. Settings exposes WeChat, Schedule and Memory
management. Use an SSH tunnel for remote browser access; the Server still
rejects non-loopback binds and invalid browser Host/Origin values.

`cairn-web`, `cairn-runtime`, `cairn-cli runtime serve`, and the public local,
standalone task/session/WeChat CLI launchers have been retired. They must not be
used to supervise another Runtime or receiver. Internal execution, recovery and
controller modules remain reusable and covered by regression tests.

## Configuration and priority

Explicit CLI options override environment variables, which override TOML, which
overrides built-in defaults. `--config` selects the file before `CAIRN_CONFIG`;
otherwise `/etc/cairn/config.toml` is used when present. Explicitly selected
missing files, unknown TOML keys, invalid types and malformed values fail.

TOML relative paths resolve against the configuration file's directory.
Environment and CLI paths must be absolute. Workspace is required; it never
silently defaults to the shell's current directory. Runtime token discovery in
the CLI uses this same configuration. `--runtime-url` and `--runtime-token-file`
remain explicit client overrides.

```toml
[server]
host = "127.0.0.1"
port = 8777
workspace = "/workspace/project"
data_root = "/var/lib/cairn"
worktrees_root = "/var/lib/cairn/worktrees"
provider_approval = "/etc/cairn/provider-approval.json"
# web_root = "/workspace/cairn/web/dist"
offline = false
allow_shell = false
allow_network = false

[budget]
max_steps = 500
max_seconds = 900.0
max_stagnant_steps = 12
# max_tokens / max_cost and input/output_cost_per_million are optional.

[services]
wechat = true
schedule = true
memory = true

[logging]
level = "info"
access_log = true
```

Server options use `CAIRN_HOST`, `CAIRN_PORT`, `CAIRN_WORKSPACE`,
`CAIRN_DATA_ROOT`, `CAIRN_WORKTREES_ROOT`, `CAIRN_WEB_ROOT`,
`CAIRN_PROVIDER_APPROVAL`, `CAIRN_OFFLINE`, `CAIRN_ALLOW_SHELL`,
`CAIRN_ALLOW_NETWORK`. Budget variables are `CAIRN_MAX_STEPS`,
`CAIRN_MAX_SECONDS`, `CAIRN_MAX_TOKENS`, `CAIRN_MAX_COST`,
`CAIRN_MAX_STAGNANT_STEPS`, `CAIRN_INPUT_COST_PER_MILLION`,
`CAIRN_OUTPUT_COST_PER_MILLION`. Services use `CAIRN_SERVICES_WECHAT`,
`CAIRN_SERVICES_SCHEDULE`, `CAIRN_SERVICES_MEMORY`; logging uses
`CAIRN_LOGGING_LEVEL` and `CAIRN_LOGGING_ACCESS_LOG`.

Without an explicit data root, the default is `$XDG_STATE_HOME/cairn`, or
`~/.local/state/cairn`. Worktrees default to `data_root/worktrees`. Server-owned
components receive explicit paths. The old independent `CAIRN_AGENT_HOME`,
`CAIRN_SCHEDULE_DIR` and WeChat/Board directory discovery do not relocate Server
state. Import existing custom roots before switching an installation.

## Provider authorization and secrets

Existing `.cairn/models.toml` and provider/model-group configuration remain
supported; see [configuration](../configuration.md). A host approval file binds
exact public routing facts before credentials are resolved:

```json
{
  "name": "example",
  "endpoint": "https://api.example.com/v1",
  "group": "primary",
  "key_env": "CAIRN_LLM_API_KEY",
  "model_ids": ["openai/your-model"]
}
```

Keep this file and Server configuration outside writable roots. The actual key
belongs in a protected environment or the existing private credential flow,
never ordinary Server TOML, Git or process argv. The unit optionally reads
`/etc/cairn/cairn.env` (0600). Missing/mismatched provider credentials reject
startup. `/model use ID` still selects only an approved model; switching provider
or changing capabilities requires host configuration and restart.

## Storage inventory and boundaries

| Relative to data_root | Owner and retained purpose |
| --- | --- |
| `server.lock` | One Server/import operation per root; kernel flock, no PID supervision |
| `runtime/runtime.token` | Private client/browser credential |
| `runtime/runtime.json`, `runtime/owner.lock` | RuntimeCatalogue task/event facts and lifetime writer ownership |
| `runtime/controls.json` | ServerControls preferences, selected model, reviewed management receipts and uncertain delivery facts |
| `runtime/sessions/sessions.sqlite3`, `-wal`, `-shm` | SessionStore checkpoints, bindings, tool fences/side effects, task ledger and recovery facts |
| `runtime/sessions/writer.lock`, `inspection.lock`, schema backup files | Existing SQLite writer/read/schema recovery protocol |
| `runtime/leases/*.lock` | Per-task execution/worktree write leases |
| `runtime/board/tasks.json`, `tasks.lock` | Shared TaskBoardStore facts and bounded update locking |
| `wechat/account.json`, `state.json`, `serve.lock`, `control.sock` | WeChat credentials, message/cursor/dedup/recovery state and receiver ownership/control |
| `schedule/jobs.json`, `runs.jsonl`, `schedule.lock`, `runner.lock` | Schedule definitions, run/receipt history and atomic claim/execution locks |
| `agent/AGENT.md`, `MEMORY.md`, `PROFILE.md`, `memory/` | AgentMemoryStore personal rules, profile, daily journal and audit history |
| `agent/pending_agent_update.json`, `PROFILE.lock`, `trusted_workspaces.json`, `trusted_workspaces.lock` | Reviewed memory updates, profile serialization and existing host trust records |
| `state-import.json` | Verified import manifest and its backup location |
| `worktrees/` (or configured external root) | Agent-writable retained Git worktrees; never treat this as host state |

The singular `schedule/` preserves the deployed default. Config is host policy;
state is persistent facts; workspace/worktrees are execution roots. Secrets are
protected environment/files, including the private WeChat account record and
Runtime token. Host state and policy paths cannot overlap either writable root.

Task diagnostics are retained in Runtime events and SessionStore. Existing
standalone trace JSONL/index files and project-memory SQLite files are not a
second production Server store; they are not deleted or implicitly converted.
The import copies retained files in selected component trees without changing
checkpoint schemas, tool fences, recovery decisions or worktree identities.
Do not delete WAL, lock, receipt or checkpoint files to make a service start.

## Existing data and explicit import

An installation already using the default root keeps its exact directories and
needs no directory migration. For an old custom `--state-dir`, set data_root to
its parent if the directory is named `runtime`. Otherwise import it explicitly.
Never point the new Server at an old live writer. Stop and inspect that service
using its own supervisor; this code does not kill it or remove ownership locks.

```sh
uv run cairn --config /etc/cairn/config.toml import-state \
  --source-root /root/.local/state/cairn \
  --backup-root /var/backups/cairn \
  --runtime-source /old/custom-runtime
```

Omit `--runtime-source` for the usual `source_root/runtime`. To import only split
management state, repeat `--component wechat --component schedule --component agent`.
For independently overridden old directories, arrange a host-owned source tree
with those component names after stopping its owner; symlinks are refused.

Source, destination and backup must be disjoint and outside writable roots.
Targets must be empty; there is no merge or overwrite. The importer acquires
Server and legacy store locks, copies a private backup, validates JSON/JSONL and
SQLite integrity, verifies SHA-256 copies, then publishes selected directories.
Ordinary failures roll back published directories; originals and backup remain.
After a machine crash during import, inspect the backup/manifest and partial
target before retrying; do not merge it into a running Server.

Keep `worktrees_root` pointing at existing retained worktrees. Moving their Git
registration or rewriting stored binding identities is outside P0-A. Imported
Runtime tasks whose worktrees do not match this location are rejected. A restart
reads existing committed history and does not replay unknown tool effects.

## Debian systemd service

After building, configuring and checking the foreground Server, install one unit:

```sh
sed \
  -e 's|__CAIRN_ROOT__|/workspace/cairn|g' \
  -e 's|__CAIRN_USER__|cairn|g' \
  -e 's|__CAIRN_CONFIG__|/etc/cairn/config.toml|g' \
  deploy/systemd/cairn.service.in > /etc/systemd/system/cairn.service
systemctl daemon-reload
systemctl enable cairn.service
./start.sh start
./start.sh status
./start.sh logs
./start.sh restart
./start.sh stop
```

Use the actual service user and grant it access to workspace, worktrees, state
and private provider environment. `CAIRN_CONFIG` and `CAIRN_SERVICE` select a
nondefault installed unit/config for the wrapper. Operations need systemd rights.
The unit supervises one process, restarts failures, sends SIGTERM and records
stdout/stderr in journald. It does not duplicate host policy in command flags.

Start/restart preflight validates configuration and installed unit identity. An
active Server must authenticate and report matching systemd MainPID and root/workspace/worktree/URL
before restart is allowed. A changed data directory/port or an old incompatible
service requires explicit inspection and stop; the wrapper will not stop it as
a side effect. `stop` is an explicit stop of the selected named unit.

## Web packaging and validation

Vite still builds `web/dist`. The build also copies assets into
`src/cairn/web/static/`, which the Python wheel includes. Installed packages find
bundled assets; source checkouts can use their built `web/dist`. All five HTML
entrypoints and the assets directory are required at Server startup. Missing
resources fail with the exact build command instead of an API-only success.
Build Web before `uv build`; no separate Web process is needed.

Focused acceptance: `uv run pytest -q tests/server tests/terminal/test_server_process.py tests/terminal/test_server_systemd.py`.
The process tests use unpaid offline models and isolated state; systemd tests
create uniquely named temporary units and never restart an operator service.
