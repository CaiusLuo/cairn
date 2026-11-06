# Cairn

Cairn is a personal Agent for Debian servers. A shared Runtime runs the Agent;
CLI, Web and WeChat clients can use the same task ID, progress, history and Git
Worktree. LangGraph orchestrates model calls and tools through the existing
`run_turn` implementation.

```text
Interactive CLI ─┐
Web Tasks ───────┼── Shared Runtime ── LangGraph model ↔ tools ── answer
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

## Start a shared Runtime

Run from the checkout. Keep private state outside the source and Worktree roots:

```sh
uv run cairn runtime serve \
  --workspace "$PWD" \
  --state-dir "$HOME/.local/state/cairn/runtime" \
  --worktrees "$HOME/.local/state/cairn/worktrees" \
  --offline --port 8777
```

In another terminal, connect the familiar interactive CLI:

```sh
uv run cairn \
  --runtime-url http://127.0.0.1:8777 \
  --runtime-token-file "$HOME/.local/state/cairn/runtime/runtime.token"
```

Chat normally, or enter `/develop Create the acceptance artifact` to exercise
real file creation with the offline model. `/status` shows the task ID; `/use ID`
attaches to an existing task. `/history`, `/continue`, `/cancel` and `/new` operate
on the same server-owned work. Exiting the client leaves server work running.

For a real model, replace `--offline` with `--provider-approval FILE` as described
in the [Runtime guide](docs/guides/runtime-service.md).

## Web and WeChat

Build the Web assets before using the browser surface:

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

Open `http://127.0.0.1:8777/tasks.html`; username is `cairn`, password is the
private `runtime.token` file. For a remote server, create an SSH tunnel first,
for example `ssh -N -L 8777:127.0.0.1:8777 user@server`. Web can submit tasks,
send follow-ups, show progress/history and perform explicit recovery.

The [WeChat bridge](docs/guides/wechat-ilink.md) connects an authorized iLink
account to that same Runtime. `/status TASK_ID` reads a task started from CLI or
Web; `/reply TASK_ID CONTENT` adds a follow-up.

## Other entry points and current limits

- `uv run cairn` still starts a local interactive Agent. It uses the same LangGraph
  turn but does not automatically connect to the shared Runtime.
- `uv run cairn runtime submit "GOAL" --token-file FILE` creates an explicit
  development task; `inspect`, `follow`, `history` and `list` read service state.
- `uv run cairn session new` and the `session inspect/reopen/reconcile` commands
  manage local durable conversations. `uv run cairn task run --help` describes the
  separate headless task entry.
- Shared Runtime currently has one task worker and a bounded queue. Its task/event
  catalogue is file-backed JSON; durable conversations use SQLite. Parallel task
  workers and a PostgreSQL backend are not implemented.
- Shared WeChat mode has no scheduler integration. The existing standalone WeChat
  route has scheduling and an optional [systemd template](docs/guides/persistent-wechat-service.md);
  a combined shared-Runtime systemd deployment is not supplied.
- Shared CLI displays tool progress and the settled answer; Web also displays
  provisional streamed answer text.

Tool permissions, credential filtering and uncertain-effect recovery remain
active. The host selects privileges. Shared Runtime shell access is opt-in and
uses the Linux project sandbox; the standalone local CLI can grant host shell
access. See [operations](docs/guides/pilot-runbook.md) before recovering work.

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
`uv run pytest`. [Tests](tests/README.md), [evaluation tools](evaluation/README.md)
and [operational scripts](scripts/README.md) have their own short indexes.
[Documentation](docs/README.md) covers configuration, running and recovery.
