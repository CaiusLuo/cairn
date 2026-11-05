# WeChat iLink text tasks

Cairn can accept text coding tasks from one personal WeChat account through the
iLink backend protocol and reply in the same conversation. This is a single
account, single Workspace, text-only first version; group chats, media, multiple
accounts/workspaces, replay and a general queue are out of scope.

## Host-private state

The bot token, poll cursor and per-message facts live in a host-private state
directory, **outside every Agent-writable Workspace**:

- default `$XDG_STATE_HOME/cairn/wechat` (usually `~/.local/state/cairn/wechat`)
- override with `--state-dir`
- directory `0700`, files `0600`
- the token is never printed, stored in the repository, sent to a model/trace,
  or put in a model request; commands only print the bot id.

A state directory inside the Workspace is refused before login saves a token or
serve reads state, creates its lock or opens a connection. An existing damaged
or unsupported-version state file fails closed instead of silently starting with
an empty dedupe map.

## Login

```sh
cairn wechat login --workspace /srv/project
```

This fetches an iLink QR code with `bot_type=3`, prints the QR content and polls
`get_qrcode_status` with the protocol cursor. `wait`/`scaned` continue,
`need_verifycode` prompts locally for the phone code, `expired` and
`verify_code_blocked` fail with a clear message, `scaned_but_redirect` moves to a
validated **https** origin, and `confirmed` saves the account token (0600) and
bot id. A missing token/id or a plaintext/credentialed/unexpected `baseurl` is a
login failure, never an optimistic success. No token is echoed.

## Serving tasks

```sh
cairn wechat serve \
  --workspace /srv/project \
  --trust-workspace \
  --allow-sender wxid_owner \
  --approve-provider-name default \
  --approve-provider-endpoint https://api.example.com/v1 \
  --approve-provider-group primary \
  --approve-provider-key-env CAIRN_API_KEY \
  --approve-provider-models "openai/model-a" \
  [--allow-shell] [--allow-mcp --mcp NAME ...] [--allow-network] \
  [--check-command "..."] [--skill NAME ...] [--state-dir ...]
```

- `--allow-sender` is required (repeatable). Only those sender ids may start an
  Agent; strangers are rejected before any effect, without a reply.
- Provider approval is the same five-fact binding as `cairn task run`; a missing
  or mismatched approval is rejected before credential resolution or a model
  request. Workspace trust, shell/MCP/network grants, check command and host
  Skill/MCP selection are also host-only. No inbound text, model output or tool
  argument can grant a capability or change the check command.
- Messages are text (`item_list type 1`) and need a stable id (`message_id`, or a
  non-empty `client_id`); otherwise nothing starts. Group chats and other media
  are unsupported.

### One task at a time, status and cancel

While a task runs, another accepted text task is durably queued rather than
rejected or silently dropped; queued work keeps arrival order and resumes after
a restart. `/status` reports whether a task is running and how many messages are
queued; `/cancel` cancels the running task and waits for its owned cleanup before
the final reply. The service also refuses to start if
another serve process holds the state-directory lock (one account/workspace).

### Dedupe, cursor and restart

`get_updates_buf` is the batch-wide cursor. Every message's processing fact is
persisted **before** the cursor advances, so a crash cannot silently lose a
received task. A duplicate message id is never executed twice. On restart, a task
already marked started is reported as interrupted and is **never** replayed; a
message persisted as received-but-not-started may be resumed once. The service
keeps processing messages while a task runs, so `/status` and `/cancel` remain
available.

## Reply and execution

Replies use the inbound message's `context_token` and the original sender id. The
task runs through the existing `run_headless_task` / `CodingTaskRunner` /
`run_turn` chain with the host-bound provider, Workspace, tools, checks and
Skills/MCP. Replies report the real machine status (`completed`,
`budget_exhausted`, `runtime_error`, `cancelled`, check status when present, a
bounded final response and trace id). Normal completion still does not mean the
code is verified unless the host check passed. Errors, trace and logs do not
contain the bot token or configured credentials.

The authorized host Shell runs as the server user with that user's access; it is
not OS-isolated and this document makes no sandbox claim.

## Current support and limits

- Only the iLink HTTP JSON protocol above is implemented; the login QR and
  getUpdates/sendMessage calls are real protocol clients.
- Offline tests use a local fake iLink HTTP server and a scripted offline model
  (including a real `edit_file` task and the same `context_token` reply); they do
  **not** prove real-WeChat interop. Real account scanning, real messages and paid
  models require separate explicit authorization.
- No group chats, media, rich messages, multi-account, multi-workspace, automatic
  replay, general scheduler or cross-platform test matrix.
- The bot token and cursor remain host-private; the inbound text is stored there
  only as a private processing fact.

Implementation: `infra/wechat/{ilink,state}.py`, `terminal/wechat/{login,serve,app}.py`;
regressions: `tests/runtime/test_wechat_service.py`, fake server
`tests/support/fake_ilink.py`.


## In-session `/wx` (recommended interactive UX)

Login is optional after `cairn` starts; it is not a prerequisite command:

```text
cairn                 # normal interactive session
/wx                   # if needed: fetch QR, render an ASCII QR in the terminal,
                      # wait for scan/verification, save account, then ensure a
                      # detached background owner receives WeChat text and schedules tasks
/wx status            # login (bot id), service state and login-status history
/wx verify <code>     # answer a phone verification code while waiting
/wx stop              # cancel login or stop receiving; finish owned cleanup
/wx allow <sender>    # add a trusted sender (login user id is trusted by default)
/wx help              # list available /wx commands
/wx disconnect        # stop service and clear local account/cursor; next /wx rescans
```

In an interactive `/wx` session the login owner is trusted by default. `/wx allow`
only appends additional task initiators; it does **not** change the scheduled
result recipient, which remains the login owner. The headless
`cairn wechat serve --allow-sender ...` entry keeps its explicit allowlist and
does not implicitly add the owner.

`/wx` without an existing account performs the login first and then ensures a
detached host background owner using the same account, state directory, provider
approval, queue and scheduler store. Closing the CLI/SSH UI detaches only; the
owner keeps receiving WeChat and running accepted work. `/wx stop` is the
explicit operation that sends SIGTERM and waits for owned cleanup. The standalone
`cairn wechat login` / `cairn wechat serve` commands remain available for
systemd/headless operation and do not create a second consumer while the
background owner is alive. A complete terminal QR is rendered from the iLink `qrcode_img_content` using
bounded half-block glyphs (quiet zone included; refuses to clip on a terminal too
narrow for the matrix); the URL is also printed as a fallback. The QR is cleared
from the composer-adjacent screen after login resolves. Standalone WeChat conversations use the existing durable SessionStore/DurableSession
path: each sender has separate stable task and chat Session IDs in one
service-owned Store writer. Each path reopens its own committed checkpoint
(including tool calls/results) before `run_turn`; closing one borrowed Session
cannot release another request's writer. Chat execution and its provider binding
both use `min(config.max_steps, 20)`, while tasks retain the configured budget.
An unreadable or unfinished Session fails closed and is never automatically
abandoned. A known task recovery requirement does not block the independent chat
Session; a genuine Store fault still blocks all durable admission.

Legacy mixed task/chat history remains under its original Session ID. An existing
purpose-specific task Session takes precedence; otherwise task work retains that
legacy owner, including its recovery gate. A newly separated chat explicitly
reports the retained legacy source ID/checkpoint instead of claiming to inherit
its full history. History forking and a WeChat host recovery UI are not yet
implemented. If a prior service created a separate `sessions-chat` database,
chat reports `chat_store_migration_required`; it does not copy, overwrite, or
silently ignore that potentially diverged history. The host must reconcile the
two histories before chat can use the shared Store. Service shutdown settles all
owned work before closing its writer. Direct hosts of `default_task_executor`
must likewise call its `close()` after settling their requests.
Session stores and conversation snapshots live under the host-private state
directory, outside the Agent-writable Workspace, effective temporary directory
and uv cache. Workspace observations use the active provider's credential
filtering for each request. Explicit shell, MCP and network options authorize
only those capabilities for the current request; grants are not restored from
a previous Session. When a configured check fails, the service permits one
repair turn and reruns the same check; it preserves the original task ID in
the result. A session cleanup failure is reported rather than treated as
success; the service retains its owner and retries cleanup before new work.

When a trusted sender's valid text task is durably accepted,
the bot sends one immediate receipt (`已收到，正在安排处理`) before execution,
without echoing the inbound prompt; it then still sends the final result. The receipt is a separate node from
the task result. Because iLink `send_text` does not provide a stable client-id
identity here, the receipt is sent once and never retried: if the response is
lost, the state records the receipt as unknown rather than risking a duplicate.
While a task runs, free-form messages are routed by a bounded semantic
classifier that returns exactly `CHAT`, `TASK`, or `UNKNOWN`; explicit commands
such as `/status` and `/cancel` never invoke that model. `CHAT` uses the same
full-capability agent tools and memory as task work, including shell, file,
MCP and extra tools subject to the active permission policy; it may also use
task tools. `TASK` remains in the durable work queue, and `UNKNOWN` keeps the
original message and asks the trusted sender to choose "查询当前任务" or
"安排新任务" without executing it. Classifier timeouts, provider errors and
malformed output are per-message failures with redacted detail; they do not
stop the WeChat service or cancel the running worker.

A busy task is durably queued instead of rejected. Queued messages keep their
arrival order and resume after a service restart; a task that was already
started is marked interrupted and is never replayed automatically. Recovery
validates every pending record before starting any task: a damaged record is a
startup error, not a silently skipped message. If recovery is cancelled or
fails after a task was admitted, the service waits for that task's owned cleanup
before releasing the serve lock. `sendTyping` progress and the final reply are
unchanged.

While a WeChat task runs, the bot sends iLink `sendTyping` (status 1) and cancels
it (status 2) when the result is sent; iLink has no token-streaming sendMessage,
so the observable progress is the typing indicator plus the final message.


## Scheduled tasks (first version)

A task created by a scheduled execution records `schedule` as its source and a
job-scoped association on the shared board. A task created by a WeChat message
records `wechat`; delivering a scheduled result through WeChat does not change
its source.

The background WeChat service accepts a bounded set of explicit one-shot
requests, such as “十分钟后提醒我检查部署结果” and
“明天上午九点检查这个项目的 CI，把结果发给我”. The host binds the original
message ID and first received time, creates the job without a classifier/model
call, and returns the saved ID, full timestamp, content and cancellation command.
The default timezone is Asia/Shanghai. Ambiguous, unsupported or expired time
expressions ask for clarification. Only the configured account owner is supported
by this first scheduling entry; other senders are explicitly rejected.

Creation is idempotent by account, workspace, sender and message ID. A restart
recovers pending creation and an unattempted receipt; a sending/unknown receipt
is not resent. Blocking storage work settles before shutdown completes. Creation
receipts have one send attempt. A reminder delivers directly without a model;
execution jobs use the existing serialized task chain. The background Agent
retains schedule list/manage tools, while free-form schedule creation remains a
host operation. The interactive CLI retains its confirmed Agent tools.

The `/schedule` commands below remain available as an admin/debug surface:

```text
/schedule add once 2026-11-01T09:00:00+08:00 <prompt>
/schedule add daily 09:00 <prompt>
/schedule add interval 3600 <prompt>
/schedule list
/schedule history [job-id]
/schedule pause <job-id>
/schedule resume <job-id>
/schedule remove <job-id>
```

Jobs and a bounded run history are stored host-privately under
`$XDG_STATE_HOME/cairn/schedule` (0700/0600), never in the Workspace. Both the interactive Cairn session and `cairn wechat serve` start the scheduler;
a due task uses the same task chain as `/wx`, then pushes its result to the
latest known WeChat context for the account owner. If no inbound context exists yet, delivery is
recorded as not delivered instead of inventing a conversation. `/schedule list` shows the last status/time and `/schedule history` shows bounded
run facts (status, task id, trace id, delivery). One task is executed at a time:
the scheduler holds a cross-process `runner.lock` across claim and execution, and
the interactive WeChat/scheduler entry points share a process task gate. The
durable next-run fact is saved at claim. The runtime runner waits for the host
execution gate, then persists the started fact immediately before entering the
executor. A cancelled claim that never entered the executor remains retryable;
a started task is not replayed after cancellation or restart. Both CLI and
background adapters pass their gate to the runner instead of marking starts
inside an executor wrapper. After acquiring the execution lock, a runner marks its own
previously started, unfinished jobs as interrupted and records delivery as
unknown; it does not infer whether a message was already sent. Waiting on the
lock is cancellable without waiting for the other process to finish.
