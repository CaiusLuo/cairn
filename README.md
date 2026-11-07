# Cairn

Cairn is a personal Agent for Debian servers. A shared Runtime runs the Agent;
CLI, Web and WeChat clients can use the same task ID, progress, history and Git
Worktree. LangGraph orchestrates model calls and tools through the existing
`run_turn` implementation.

```text
Interactive CLI ─┐
Web Chat ────────┼── Shared Runtime ── LangGraph model ↔ tools ── answer
WeChat bridge ──┘        │
                   tasks / history / recovery / worktrees
```

Ordinary chat uses read-only tools without allocating a Worktree. When the model
requests development, the Runtime creates one and keeps the same task ID and
prior history. Explicit development commands skip that transition. The service
persists task state, enforces host permissions and budgets, and retains work for
safe continuation.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Development tasks need
Git 2.40+ for Worktree operations. Durable storage also checks the SQLite library
linked to Python; see [operations](docs/guides/pilot-runbook.md).

```sh
git clone https://github.com/CaiusLuo/cairn.git
cd cairn
uv sync --locked --all-groups
```

For real models, configure `.env` or `.cairn/models.toml` using
[configuration](docs/configuration.md). The offline example below needs no model
credentials and makes no paid API requests.

## Start Cairn Server

Build Web, copy [the host configuration](deploy/config.example.toml) to
`/etc/cairn/config.toml`, and set its workspace, data paths and provider approval:

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run cairn --config /etc/cairn/config.toml
```

For an unpaid offline smoke test, remove `provider_approval` and set
`offline = true`. Production never silently falls back to an offline model.
From the configured workspace, connect the independent client:

```sh
uv run cairn-cli --config /etc/cairn/config.toml
```

Open `http://127.0.0.1:8777/chat.html`; username is `cairn`, password is
`data_root/runtime/runtime.token`. Web Chat, Tasks, History, WeChat management
and Schedule share the same Server. Use an SSH tunnel for a remote server.

Chat normally, or enter `/develop GOAL`. `/status`, `/history`, `/continue`,
`/cancel`, `/new` and `/use ID` operate on server-owned work. `/wx` starts login;
`/wx status` and `/wx verify CODE` manage it. Web Settings offers these same
controls plus Schedule and Memory. Client exit leaves background services running.

The [Server guide](docs/guides/runtime-service.md) covers configuration priority,
persistent files, explicit backed-up data import, provider authorization and
systemd installation. After installing the unit, `./start.sh` supports
`start`, `stop`, `restart`, `status` and `logs`; supervision belongs to systemd.
Standalone CLI/WeChat Runtime launchers have been retired.

One task worker and a bounded queue remain; Runtime facts use JSON and durable
Sessions use SQLite. Tool permissions, credential filtering, LangGraph execution
and uncertain-effect recovery remain active. PostgreSQL, parallel workers and
Conversation/Task/Run separation are outside this change.

## Development

Use current code and behavioral tests to guide changes. Architecture and storage
can evolve with the product; this documentation describes the implementation.

```sh
uv run pytest -q tests/docs
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Run the regression tests for the code you change; the full offline suite is
`uv run pytest`. [Tests](tests/README.md) and
[operational scripts](scripts/README.md) have their own short indexes.
[Documentation](docs/README.md) covers configuration, running and recovery.
