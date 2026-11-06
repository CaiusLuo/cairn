# Shared Runtime

One service owns task admission, a bounded queue, one task worker, cancellation
and public event history. Clients submit or observe tasks; disconnecting a client
does not stop the Agent. TaskBoard is a projection: moving a card does not start
or resume execution.

## Start

Install dependencies from the [README](../../README.md#install). For a real model,
prepare [configuration and provider approval](../configuration.md), then run:

```sh
uv run cairn runtime serve \
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
`pnpm --dir web build`. Open `http://127.0.0.1:8777/tasks.html` through an SSH
tunnel. Login uses username `cairn` and the Runtime token as password.

## Interactive CLI and batch commands

```sh
uv run cairn --runtime-url http://127.0.0.1:8777 \
  --runtime-token-file "$HOME/.local/state/cairn/runtime/runtime.token"
uv run cairn --runtime-url http://127.0.0.1:8777 \
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
| `/exit` | Detach the client |

Input typed during execution waits for the next settled turn. Ctrl+C on the
running terminal requests cancellation; closing the client only detaches.
Shared mode uses the native composer and transcript without creating a local
Agent. `uv run cairn` without Runtime options remains a separate local session.

Batch commands accept `--url` (default `http://127.0.0.1:8777`) and the required
`--token-file FILE`:

```sh
uv run cairn runtime submit "Create the acceptance artifact" --token-file FILE
uv run cairn runtime list --token-file FILE
uv run cairn runtime inspect TASK_ID --token-file FILE
uv run cairn runtime follow TASK_ID --token-file FILE
uv run cairn runtime history TASK_ID --token-file FILE
uv run cairn runtime cancel TASK_ID --token-file FILE
uv run cairn runtime continue TASK_ID --token-file FILE
```

`submit` creates a development task. With the offline model it writes
`runtime-result.txt` in a real isolated Worktree. Ordinary offline chat returns a
scripted answer; adaptive model tool choice is not demonstrated by that answer.

## Chat and development

Web and interactive CLI default to conversation mode; plain WeChat messages reuse
a conversation per bot/sender. Chat has `read_file`, `glob`, `grep` and the
`begin_development` control tool, without an initial repository scan or Worktree.
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
throughput. Parallel workers, a PostgreSQL backend, shared scheduling and
a combined systemd deployment are not implemented.

The [WeChat bridge](wechat-ilink.md) uses these same tasks. The standalone local
CLI and standalone WeChat service still exist and use the same Agent turn; the
latter retains its own routing/scheduler. Those are separate entry modes, not a
second LangGraph model/tool implementation.

## Implementation and HTTP API

Current owners are `src/cairn/runtime/task_service.py`,
`src/cairn/runtime/service_execution.py` and `src/cairn/runtime/loop.py`.
The model/tools graph is built in `run_turn`; SessionStore/DurableSession handle
effect receipts and recovery. LangGraph is not the permission authority.

Endpoints under `/api/runtime/tasks` support list/submit, `ID`, `ID/messages`,
`ID/events?after=SEQ`, `ID/history`, `ID/stream?after=SEQ`, `ID/cancel`,
`ID/continue`, `ID/recovery` and `ID/reconcile`. They require the private bearer
token or the Web host's authenticated cookie/Origin checks. Follow-up input
carries a stable request ID and expected task sequence to reject stale writes
and deduplicate lost acknowledgements. Reads do not execute tools.
