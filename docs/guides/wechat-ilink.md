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

While a task runs, another accepted text task gets an explicit busy reply and is
recorded as busy (not silently dropped and not queued). `/status` reports whether
a task is running and for how long; `/cancel` cancels the running task and waits
for its owned cleanup before the final reply. The service also refuses to start if
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
                      # wait for scan/verification, save account, then start
                      # receiving WeChat text and scheduling tasks in this process
/wx status            # login (bot id), service state and login-status history
/wx verify <code>     # answer a phone verification code while waiting
/wx stop              # cancel login or stop receiving; finish owned cleanup
/wx allow <sender>    # add a trusted sender (login user id is trusted by default)
```

In an interactive `/wx` session the login owner is trusted by default. `/wx allow`
only appends additional task initiators; it does **not** change the scheduled
result recipient, which remains the login owner. The headless
`cairn wechat serve --allow-sender ...` entry keeps its explicit allowlist and
does not implicitly add the owner.

`/wx` without an existing account performs the login first and then starts the
same single-task WeChat service; `/wx stop` stops it. The service uses the
current session's provider/runtime and the same `run_headless_task`/`run_turn`
chain. The standalone `cairn wechat login` / `cairn wechat serve` commands remain
for headless/server operation. A complete terminal QR is rendered from the iLink `qrcode_img_content` using
bounded half-block glyphs (quiet zone included; refuses to clip on a terminal too
narrow for the matrix); the URL is also printed as a fallback. The QR is cleared
from the composer-adjacent screen after login resolves. While a WeChat task runs,
the bot sends iLink `sendTyping` (status 1) and cancels it (status 2) when the
result is sent; iLink has no token-streaming sendMessage, so the observable
progress is the typing indicator plus the final message.


## Scheduled tasks (first version)

Host-authored tasks can be created from the interactive session:

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
durable next-run fact is saved before execution, so a restart does not replay a
started task. After acquiring the execution lock, a runner marks its own
previously started, unfinished jobs as interrupted and records delivery as
unknown; it does not infer whether a message was already sent. Waiting on the
lock is cancellable without waiting for the other process to finish.
