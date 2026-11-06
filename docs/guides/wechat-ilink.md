# WeChat

Cairn uses iLink text messages from explicitly trusted senders. Login and receiver
state are host-private; never put account tokens in Git, prompts or service argv.

## Server-owned login (recommended)

Start the [shared Runtime](runtime-service.md), then run `uv run cairn-cli` in
its workspace and enter `/wx`. The CLI displays the QR and login status;
`/wx verify CODE` supplies a requested code. Web `/settings.html` offers the same
login, status, trust and stop controls. Closing a client does not stop the
Server receiver. `/wx stop` retains login; `/wx disconnect` clears it.

The bound owner can use `/schedule` and `/memory` commands, and ordinary Agent
turns have the same scheduling and memory tools as CLI/Web. Rule approval still
requires explicit review in CLI/Web. Completed task replies return through the
original account; uncertain delivery is retained without automatic replay.

## Separate shared Runtime bridge (migration)

For a separately supervised receiver, start the shared Runtime first. Complete
QR login in a foreground terminal, then start one receiver using the same saved
account/state as the Server controls. Stop any Server-owned receiver first:


```sh
uv run cairn-cli wechat login --workspace /srv/project \
  --state-dir "$HOME/.local/state/cairn/wechat"
uv run cairn-cli wechat serve --workspace /srv/project \
  --state-dir "$HOME/.local/state/cairn/wechat" \
  --trust-workspace --allow-sender YOUR_SENDER_ID \
  --runtime-url http://127.0.0.1:8777 \
  --runtime-token-file "$HOME/.local/state/cairn/runtime/runtime.token"
```

The Runtime owns provider approval, task budgets, tools and Worktrees. The bridge
checks sender/context/message identity before forwarding; it does not construct
a local Agent or run the standalone classifier and scheduler.

| Message | Shared-mode behavior |
| --- | --- |
| Ordinary text | Open or continue this bot/sender's conversation |
| `/task GOAL` | Create an explicit development task |
| `/status` or `/status TASK_ID` | List recent tasks or inspect the specified task |
| `/reply TASK_ID CONTENT` | Add a follow-up to an existing shared task |
| `/history TASK_ID` | Read recent committed conversation |
| `/continue TASK_ID` | Continue safely paused work |
| `/cancel TASK_ID` | Request cancellation |
| `/recovery TASK_ID` | Read the recovery report |

Use the task ID printed by CLI or Web to query the same task from WeChat.
Stable incoming message IDs deduplicate delivery retries. If the sender's
conversation is busy or needs recovery, a new ordinary message is explicitly
reported as not executed; resend it after settlement. Reconciliation stays with
the authenticated host CLI/Web, after inspecting effects.

Shared schedule/memory commands are handled by the Server for the bound owner.
Pending standalone queue records are
refused rather than silently converted to shared tasks. Keep an existing
standalone account/state owner separate while migrating; do not start a second
receiver against a live owner's state or delete its lock file.

## Standalone mode

Without `--runtime-url`, `cairn-cli wechat serve` runs its existing local routing,
Agent execution and scheduler. It requires `--trust-workspace`, a trusted sender
and the five provider approval facts matching [configuration](../configuration.md):

```sh
uv run cairn-cli wechat serve --workspace /srv/project \
  --trust-workspace --allow-sender YOUR_SENDER_ID \
  --approve-provider-name example \
  --approve-provider-endpoint https://api.example.com/v1 \
  --approve-provider-group primary \
  --approve-provider-key-env CAIRN_LLM_API_KEY \
  --approve-provider-models openai/your-model
```

Host flags explicitly enable shell, MCP or network capabilities when needed;
`uv run cairn-cli wechat serve --help` lists the supported options.
Standalone scheduling commands include:

```text
/schedule add once 2027-01-01T09:00:00+08:00 Remind me to review Cairn
/schedule add daily 09:00 Review today's tasks
/schedule add interval 3600 Check progress
/schedule list
/schedule history
/schedule pause JOB_ID
/schedule resume JOB_ID
/schedule remove JOB_ID
```

Specify an offset for an unambiguous once-only time. Do not recreate an
interrupted job blindly: execution or delivery may already have happened.

In `cairn-cli local`, `/wx`, `/wx status`, `/wx allow SENDER`, `/wx stop`
and `/wx disconnect` manage the standalone detached owner. Closing SSH or the CLI
detaches; `/wx stop` requests owned shutdown. This flow does not automatically
attach to the shared Runtime. For systemd supervision and host logs, see
[deployment](persistent-wechat-service.md).
