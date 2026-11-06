# Shared Runtime

One service owns task admission, a bounded queue, one task worker, cancellation
and public event history. Clients submit or observe tasks; disconnecting a client
does not stop the Agent. TaskBoard is a projection: moving a card does not start
or resume execution.

## Start

Install dependencies from the [README](../../README.md#install). For a real model,
prepare [configuration and provider approval](../configuration.md), then run:

```sh
uv run cairn \
  --workspace /srv/project \
  --state-dir "$HOME/.local/state/cairn/runtime" \
  --worktrees "$HOME/.local/state/cairn/worktrees" \
  --provider-approval "$HOME/.local/state/cairn/policy/provider.json" \
  --port 8777
```

Use `--offline` in place of `--provider-approval FILE` for scripted acceptance.
The server creates a private `runtime.token` under its state directory and binds
only `127.0.0.1`. State and policy files belong outside all Agent-writable roots;
see [operations](pilot-runbook.md). Use a separate port and state for experiments.

Build Web assets with `pnpm --dir web install --frozen-lockfile` and
`pnpm --dir web build`. Open `http://127.0.0.1:8777/chat.html` through an SSH
tunnel. Login uses username `cairn` and the Runtime token as password.

## Interactive CLI and batch commands

```sh
uv run cairn-cli --runtime-url http://127.0.0.1:8777 \
  --runtime-token-file "$HOME/.local/state/cairn/runtime/runtime.token"
uv run cairn-cli --runtime-url http://127.0.0.1:8777 \
  --runtime-token-file "$HOME/.local/state/cairn/runtime/runtime.token" \
  --task-id TASK_ID
```

| In-session command | Action |
| --- | --- |
| `/new` | Next input starts a new conversation |
| `/develop GOAL` | Create an explicit development task |
| `/use TASK_ID` | Attach and display committed replies |
| `/status`, `/history` | Read the selected task |
| `/continue`, `/cancel` | Continue a safely paused task or request cancellation |
| `/wx`, `/wx status`, `/wx verify CODE`, `/wx stop` | Manage Server-owned login and receiver |
| `/schedule help`, `/schedule list`, `/schedule history` | Manage shared scheduled tasks |
| `/memory note TEXT`, `/memory pending`, `/memory approve` | Record personal memory and approve the reviewed candidate |
| `/model list`, `/model use ID` | Select only within the host-approved model group |
| `/task add`, `/task todo`, `/task done`, `/task progress` | Update the existing shared TaskBoard without starting execution |
| `/help` | Read the Server command catalogue used by terminal completion |
| `/exit` | Detach the client |

Ordinary input typed during execution waits for the next settled turn.
Management commands and `/status` remain usable while the Agent is busy. Ctrl+C on the
running terminal requests cancellation; closing the client only detaches.
Shared mode uses the native composer and transcript without creating a local
Agent. `uv run cairn-cli local` is the explicit standalone migration mode.

The default URL is `http://127.0.0.1:8777`. Server state defaults to
`$XDG_STATE_HOME/cairn/runtime` (or `~/.local/state/cairn/runtime`), and Worktrees
to the sibling `worktrees` directory. The client reads `runtime.token` from that
same default state directory. For custom state/ports, use `--runtime-url` and
`--runtime-token-file`, or `CAIRN_RUNTIME_URL` / `CAIRN_RUNTIME_TOKEN_FILE`.

The client compares its canonical current directory with the authenticated
`/api/runtime/workspace` identity before entering chat. This version supports one
exact workspace root; a different directory is refused. From a different project,
use an installed `cairn-cli` or `uv run --project /workspace/cairn cairn-cli` to use
Cairn's Python environment while preserving the caller's working directory.
Each task request also
carries that workspace as a file URI in `X-Cairn-Workspace`, so a service restarted
on another project cannot silently accept existing client input.

Historical `cairn runtime/session/task/wechat` commands move to
`cairn-cli runtime/session/task/wechat`; `cairn-runtime` remains a Server alias.
Running the Server in an SSH foreground terminal does not detach it: use systemd
for a persistent supervisor.

Batch commands accept `--url` (default `http://127.0.0.1:8777`) and the required
`--token-file FILE`:

```sh
uv run cairn-cli runtime submit "Create the acceptance artifact" --token-file FILE
uv run cairn-cli runtime list --token-file FILE
uv run cairn-cli runtime inspect TASK_ID --token-file FILE
uv run cairn-cli runtime follow TASK_ID --token-file FILE
uv run cairn-cli runtime history TASK_ID --token-file FILE
uv run cairn-cli runtime cancel TASK_ID --token-file FILE
uv run cairn-cli runtime continue TASK_ID --token-file FILE
```

`submit` creates a development task. With the offline model it writes
`runtime-result.txt` in a real isolated Worktree. Ordinary offline chat returns a
scripted answer; adaptive model tool choice is not demonstrated by that answer.

## Chat and development

Web and interactive CLI default to conversation mode; plain WeChat messages reuse
a conversation per bot/sender. Chat has read-only project tools, on-demand skills, personal memory, scheduling
and `begin_development`, without an initial repository scan or Worktree.
The model can answer directly or request development; no keyword classifier is
used on this shared path.

After the tool group settles, the Runtime commits the chat turn and creates the
development Worktree with host-approved permissions. The task ID stays the same.
The new workspace gets a new durable Session; prior checkpoint receipts establish
history provenance. Later turns and restarts retain the development Session and
Worktree. Earlier file observations need rechecking against the new workspace.
The automatic transition uses the remaining budget, without replenishing it.

Web supports same-task follow-ups, history, events, cancel, continue and recovery.
The CLI shows tool progress and the settled answer; Web additionally streams
provisional answer text. Committed history is the recovery source of truth.

## Budgets

All shared clients use the host policy. Defaults are 500 steps, 900 seconds and
12 repeated unchanged tool batches. Pass `--budget-config FILE` for a private
TOML file outside writable roots:

```toml
[budget]
max_steps = 1000
max_seconds = 900
max_tokens = 200000
max_stagnant_steps = 12
```

Optional `max_cost`, `input_cost_per_million` and `output_cost_per_million` enable
a host-rate cost estimate. Time/step/token/cost stops preserve results and pause
work. One in-flight response can exceed a token threshold; unknown usage remains
unknown. This is not a billing reservation. The durable per-turn effect limit
also pauses before a further effect.

Explicit continue grants a new budget window while retaining cumulative usage.
Unfinished or uncertain effects require [recovery review](pilot-runbook.md#interrupted-work)
first. `recovery_required` is not permission to replay a tool.

## Host permissions and current limits

File tools protect credentials and private Runtime state. `--allow-shell` enables
the existing Linux project shell boundary, requiring bubblewrap and usable
namespaces. Missing isolation fails closed. The sandbox shell has network access;
`--allow-network` separately grants network-tool capability. Clients and model
arguments cannot grant either permission.

The current catalogue stores task/event facts in JSON with an exclusive process
lease; durable checkpoints use SQLite. The single worker and full catalogue rewrites limit
throughput. Parallel workers and a PostgreSQL backend are not implemented.

The [WeChat bridge](wechat-ilink.md) uses these same tasks. The standalone local
CLI and standalone WeChat service still exist and use the same Agent turn; the
latter retains its own routing/scheduler. Those are separate entry modes, not a
second LangGraph model/tool implementation.

## Shared controls

The default Server home opens `/chat.html`. `/tasks.html` remains the TaskBoard,
`/index.html` is committed Session history, and `/settings.html` manages WeChat,
schedules and memory. All clients use the same authenticated Server API.

The Server reuses the existing WeChat, schedule and memory state locations under
`$XDG_STATE_HOME/cairn` (normally `~/.local/state/cairn`). The existing
`CAIRN_AGENT_HOME` override still selects personal memory. These roots must live
outside the project and Worktrees and are protected from Agent file/shell tools.
An already-running standalone WeChat owner must stop through its own controller
before the shared receiver can acquire that state; never remove its live lock.

`/wx` reuses a saved account or starts QR login. `/wx login` requests a new login
when stopped. `/wx stop` retains the account; `/wx disconnect` removes it and
clears extra trusted senders. Desired receiver state is persisted for Server
restart. QR and verification values are transient, never stored as credentials
in the control journal. The Server sends accepted WeChat tasks' final results to
the same account/sender; an uncertain push is not retried automatically.

```text
/schedule remind once 2027-01-01T09:00:00+08:00 Review Cairn
/schedule add once 2027-01-01T09:00:00+08:00 Inspect project status
/schedule list
/schedule history
/memory note Prefer short Chinese replies
/memory propose Keep replies concise
/memory pending
/memory approve
```

`remind` sends the text without a model call. `add` submits an Agent task to the
same Runtime and reports its task ID in run history. Existing daily/interval,
pause/resume/remove operations remain available; daily time uses Asia/Shanghai.
Reminders do not wait for the Agent worker. Never-started due jobs catch up once;
started executions and uncertain deliveries are not silently replayed.

Task journaling runs outside the reply path. Existing memory tools verify trusted
user text and do not grant permissions. The bound WeChat owner can manage their
notes/plans; additional trusted project senders do not receive personal-memory
or schedule tools. Candidate rule approval stays in CLI/Web and requires the
exact proposal just reviewed. Read `/memory pending` before `/memory approve`;
Web shows the candidate and an explicit checkbox. A changed candidate requires
another review. Nothing automatically approves new behavior or permissions.

`/provider` and `/mcp` report host configuration; switching providers, changing
credentials/model configuration or enabling MCP requires host approval/restart.
Shared `/compact` explicitly reports unavailable; it never claims a manual
compression happened. Automatic context compaction remains in the Agent turn.
The explicit legacy `cairn-cli local` retains its original standalone commands.

Management writes carry stable request IDs. A disconnected HTTP client does not
cancel a Server-owned operation. Completed receipts deduplicate retries; after a
crash, pending writes need inspection in Web or `/management acknowledge ID`
(the latter records review without replay). This does not block ordinary chat
or unrelated commands. Existing Session recovery remains a separate review.

## Implementation and HTTP API

Current owners are `src/cairn/runtime/task_service.py`,
`src/cairn/runtime/service_execution.py` and `src/cairn/runtime/loop.py`.
Server composition and control ownership live in `src/cairn/server.py` and
`src/cairn/server_controls.py`. The model/tools graph is built in `run_turn`; SessionStore/DurableSession handle
effect receipts and recovery. LangGraph is not the permission authority.

`GET /api/runtime/management?section=wechat|schedule|memory` exposes state;
`GET /api/runtime/commands` exposes the actual command menu, and `POST` on that
path invokes a control. Browser management writes require the exact same Origin;
Bearer clients remain workspace-bound.

Endpoints under `/api/runtime/tasks` support list/submit, `ID`, `ID/messages`,
`ID/events?after=SEQ`, `ID/history`, `ID/stream?after=SEQ`, `ID/cancel`,
`ID/continue`, `ID/recovery` and `ID/reconcile`. They require the private bearer
token or the Web host's authenticated cookie/Origin checks. Follow-up input
carries a stable request ID and expected task sequence to reject stale writes
and deduplicate lost acknowledgements. Reads do not execute tools.

## Debian systemd service

`deploy/systemd/cairn.service.in` supervises the existing Server, with one
Runtime state lease, SIGTERM cleanup and `Restart=on-failure`. Render it using
an existing service user and absolute paths:

```sh
sed \
  -e 's|__CAIRN_ROOT__|/workspace/cairn|g' \
  -e 's|__CAIRN_USER__|cairn|g' \
  -e 's|__CAIRN_HOME__|/home/cairn|g' \
  -e 's|__CAIRN_XDG_STATE_HOME__|/home/cairn/.local/state|g' \
  -e 's|__CAIRN_WORKSPACE__|/srv/project|g' \
  -e 's|__CAIRN_STATE__|/var/lib/cairn/runtime|g' \
  -e 's|__CAIRN_WORKTREES__|/var/lib/cairn/worktrees|g' \
  -e 's|__CAIRN_PROVIDER_APPROVAL__|/etc/cairn/provider.json|g' \
  -e 's|__CAIRN_PORT__|8777|g' \
  deploy/systemd/cairn.service.in | sudo tee /etc/systemd/system/cairn.service
sudo systemctl daemon-reload
sudo systemctl enable --now cairn
sudo systemctl status cairn
sudo journalctl -u cairn -f
```

Prepare private state parent directories writable by that service user. The
provider approval file matches the workspace configuration; credentials stay in
the workspace `.env` or a private `/etc/cairn/cairn.env`, not the unit or argv.
For the custom state example, connect using `--runtime-token-file
/var/lib/cairn/runtime/runtime.token` as an authorized user with access to that
private file. The default client and Server paths otherwise follow XDG together.

`systemctl stop cairn` requests owned cleanup; `systemctl restart cairn` preserves
stored task facts and Worktrees. A crash during an unfinished effect still needs
explicit recovery review, not an automatic replay. `WantedBy=multi-user.target`
allows startup on boot when enabled. A foreground `uv run cairn` remains owned by
that foreground session; it is not a daemon.

The template is opt-in. Installing it does not migrate or stop existing
WeChat/Web processes. The standalone [WeChat unit](persistent-wechat-service.md)
now uses `cairn-cli wechat serve`; render that updated unit before upgrading an
old deployment's command path. Do not run two receivers for one account/state.
