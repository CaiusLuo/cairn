<p align="center">
  <img src="assets/cairn-github-readme.png" alt="Cairn — agent harness" width="720">
</p>

Cairn is a personal Agent for Debian servers. A shared Runtime runs the Agent;
CLI, Web and WeChat clients can use the same Conversation/Run identity, progress,
history and (for explicit development) Git Worktree. LangGraph orchestrates model calls and tools through the existing
`run_turn` implementation.

```text
Interactive CLI ─┐
Web Chat ────────┼── Shared Runtime ── LangGraph model ↔ tools ── answer
WeChat bridge ──┘        │
                   tasks / history / recovery / worktrees
```

Ordinary chat creates durable Conversation and Run facts in the DomainStore,
uses read-only tools without allocating a Task or Worktree, and never appears in
TaskBoard. Explicit development remains on the compatibility task path during
B2; it creates a Worktree and TaskBoard card under the existing safety rules, and
the complete Task lifecycle move is B3. The service persists state, enforces host
permissions and budgets, and retains work for safe continuation.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Development tasks need
Git 2.40+ for Worktree operations. Durable storage also checks the SQLite library
linked to Python.

```sh
git clone https://github.com/CaiusLuo/cairn.git
cd cairn
uv sync --locked --all-groups
```

For real models, configure `.env` or `.cairn/models.toml`. The offline example
below needs no model credentials and makes no paid API requests.

## Start Cairn Server

After preparing your model configuration, build Web and start the background
service on Debian:

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
sudo ./scripts/start.sh
```

On first use, the script creates missing `/etc/cairn/config.toml`, provider
approval and systemd unit files. It reuses your `.env` and `.cairn/models.toml`,
checks configuration and Web assets, and reports the running PID. Existing host
files are preserved. `sudo ./scripts/end.sh` reports the PID and stops the service.
Foreground debugging remains `uv run cairn --config /etc/cairn/config.toml`.

For an unpaid offline smoke test, remove `provider_approval` and set
`offline = true`. Production never silently falls back to an offline model.
From the configured workspace, connect the independent client:

```sh
uv run cairn-cli --config /etc/cairn/config.toml
```

Open `http://127.0.0.1:8777/chat.html`; username is `cairn`, password is
`data_root/runtime/runtime.token`. One sign-in lasts 30 days and survives
service restarts; the browser cookie is backed by hashed sessions in
`data_root/runtime/web-sessions.json`. The Web Workbench shares this Server for conversations, development tasks, run
history, execution recovery and private knowledge clips. Settings manages WeChat, schedules and memory. Knowledge clips
are stored outside the Agent workspace and are never injected into ordinary Agent turns or `MEMORY.md`. Use an SSH tunnel for a remote server.

Daily reviews are generated from the day's clips, Task/Run receipts and personal journal through
`POST /api/knowledge/reviews/daily`; `GET /api/knowledge/reviews` and
`GET /api/knowledge/reviews/{id}` return the persisted report, its source manifest and the bounded redacted
source snapshots used at generation time. Archived clips are excluded. Generation uses one
restricted provider call with no tools and sends only a bounded, redacted source snapshot. Repeated requests
with the same `request_id`, date, timezone and regeneration mode are idempotent even if sources changed in
between; a reused id with different intent is rejected. A completed report is never overwritten, and explicit
regeneration creates a new version.

The Server can also schedule the daily review itself, disabled by default. Enable it in `config.toml`:

```toml
[review]
enabled = true
time = "20:30"
# Weekly reports are independent of the daily schedule; each has its own
# durable cursor. Sunday 20:40 summarizes the seven daily windows ending at
# Sunday's 20:30 cutoff.
weekly_enabled = true
weekly_time = "20:40"
timezone = "Asia/Shanghai"
# Required when either schedule is enabled: the only WeChat user ID allowed
# to receive the scheduled report. It is host authorization, not whichever
# account is logged in when the report is due.
recipient = "wechat-user-id"
# Optional additional pin for the bot account.
# account_id = "ilink-bot-id"
```

It covers the contiguous window from the previous day's cutoff to the current one. Enabling the schedule for
the first time before today's cutoff starts with the current period and never sends yesterday's report; a
restart keeps its persisted cursor. After a longer shutdown it compensates at most three unclaimed cutoff
days and delivers only the current period; older periods are recorded in `skipped_days` rather than generating
a burst of stale WeChat messages. Failed or crash-interrupted generations are recorded per day in
`failed_days` and are never retried automatically; `POST /api/knowledge/reviews/schedule/retry` with
`{"day": "YYYY-MM-DD"}` is the explicit, auditable retry path.

A scheduled completed review gets a durable delivery record bound to the configured recipient. It is pushed
to WeChat as plain mobile-sized text only when that exact recipient is logged in and has a fresh inbound
reply context; otherwise it stays `pending` or `blocked_offline`. A different logged-in user is never used as
a fallback. The ledger distinguishes `sending`, `accepted`, `delivery_unknown` and `failed_permanent`; an
unknown result is never resent automatically. iLink may refuse a send with `ret=-2` once the last inbound
reply context is no longer accepted; that is a definite non-send, so the delivery stays retryable and is sent
after the next inbound WeChat message provides a fresh context.

Weekly reports use the same store and delivery outbox. `POST /api/knowledge/reviews/weekly` with
`{"week_id": "YYYY-Www", "request_id": ...}` generates one report for the seven daily cutoff windows
ending at that ISO week's Sunday 20:30 boundary; `GET /api/knowledge/reviews?type=weekly` lists them.
The request is refused with `review_period_not_ended` until that boundary has passed, so manual
generation cannot open an unfinished week. Completed daily reports are secondary evidence and never
widen what their original Task/Clip/Run sources can prove. A source omitted from a daily report stays
omitted, and an identity-only manifest entry can never prove completion. When one Task or Clip changes
during the week, the latest included version determines its state while every version hash remains in
the snapshot for audit. Missing daily reports are marked and that day's raw records are collected instead.
A week with no records still completes with an explicit no-record note. The schedule has its own
persisted cursor and generates at most the most recent week after an outage. First enablement never
backfills earlier weeks: it starts with the next Sunday boundary, or with the week that just ended if
enabled on Sunday after `weekly_time`. Failures are left for
`POST /api/knowledge/reviews/schedule/weekly/retry` with
`{"week_id": "...", "request_id": "..."}`. Replaying a retry `request_id` returns the same review
without a second model call or delivery intent; a new request ID is a new operator confirmation.
Delivery stays idempotent per weekly version and only goes to the host-authorized recipient.

`POST /api/knowledge/reviews/{id}/delivery/resend` requires a caller-supplied `request_id` and persists a
receipt for the accepted or rejected operation. Replaying the same `request_id` returns that receipt without a
second send, so a lost HTTP response is safe to retry; a new operator confirmation must use a new
`request_id`. `{"rebind": true}` additionally re-points the delivery at the configured host target and is
refused when review notifications have no configured recipient. Delivery never changes the review's
completed state.

Chat normally, or enter `/develop GOAL`. `/status`, `/history`, `/cancel`,
`/new` and `/use CONVERSATION_ID` operate on the same server-owned Conversation;
`/continue` and development task controls remain on the explicit development path.
`/use TASK_ID` and `/task use TASK_ID` switch the client to that explicit Task,
while `/use RUN_ID` selects a Conversation Run; appended input keeps the selected
Task, Session and Worktree.
Conversation creation and messages accept a stable `request_id`; retries return
the original Conversation/Run and never fork a duplicate. A Development Task
created from Web or CLI can be continued from either entry by Task ID. Each
appended input keeps its own `request_id`, and the Task exposes durable per-input
receipts with the settled event sequence, result excerpt and retained
Session/Worktree identity. Paused and completed Tasks accept another input in
place; active turns and recovery-required Tasks are refused with distinct
conflict responses, and failed/cancelled Tasks require a new Task.
Run cancellation is a `POST /api/runtime/runs/{run_id}/cancel`; recovery review uses
`GET /api/runtime/runs/{run_id}/recovery` and
`POST /api/runtime/runs/{run_id}/reconcile`.
`/wx` starts login;
`/wx status` and `/wx verify CODE` manage it. Web Settings offers these same
controls plus Schedule and Memory. Client exit leaves background services running.

`./scripts/start.sh` supports `start`, `stop`, `restart`, `status` and
`logs`; supervision belongs to systemd. Host setup and service operations need
root or equivalent systemd rights.
Standalone CLI/WeChat Runtime launchers have been retired.

One task worker and a bounded queue remain. Ordinary chat facts use SQLite
DomainStore; development execution still uses the RuntimeCatalogue compatibility
representation until B3. Durable Sessions use SQLite. Tool permissions,
credential filtering, LangGraph execution and uncertain-effect recovery remain
active. Run events are delta-batched and replayed from an `after` cursor. Web
Chat and CLI consume pages until the true final sequence; WebSocket clients
reconnect with the same cursor and receive a bounded preview snapshot for
in-flight text. Offline migration is available through the `migrate-domain`
command with backup and source snapshot checks. PostgreSQL and parallel workers
remain outside this change.

For ordinary WeChat reminders, use `/schedule history JOB_ID` to see the result of
each occurrence. A missing inbound WeChat context or an explicit iLink rejection
is recorded as a delivery failure, with an iLink return code when available.
Each reminder has at most three send attempts. Cairn checks again after 5 seconds
and then 15 more seconds when it can confirm the request did not go out. If iLink
explicitly refuses with `ret=-2`, it only tries again if a new inbound context
is available by one of those checks. The history includes the number of attempts.
A send with an unknown outcome is recorded as `delivery_unknown` and is never
retried automatically.

## Channels (C0 connection core)

`src/cairn/channels` is the shared connection foundation for WeChat, QQ, Telegram
and Discord. It implements configuration, private credential storage, lifecycle,
health, inbound dedupe, outbound delivery audit and durable source replies.
Telegram has a real aiogram long-polling adapter; QQ and Discord are
configuration-only for now, so `start` or `reconnect` for them returns
`adapter_not_implemented` (HTTP 501). Such a connection never reports `running`,
its platform summary stays `silent`, and it remains editable and deletable. The
deterministic Fake Adapter is injected only by tests.

The independent Web connection console at `/channels.html` shows the four fixed
platforms, then one platform page with its connection list and per-connection
actions. Create and edit open a centered dialog, so there is no third-level page;
`platform` is the only URL parameter, and direct entry, refresh and browser
history keep the same platform view. Forms come from the Server-authored
`GET /api/channels/capabilities` schema; a platform without a registered adapter
can save configuration but cannot start, and the console never invents a running
state. Credentials remain write-only, and every mutation uses the same request
IDs and revisions as the API.

WeChat remains the existing single host-owned session. The channel API projects
that same `WechatSessionController` (QR login, Conversation/Run, Scheduler and
daily-review delivery are unchanged) and forwards start/stop/reconnect to it.
Do not create a second WeChat client for the same account: one Server owns one
WeChat session, and the channel layer never persists or replaces its login state.
The legacy connection is read-only through the channel API; use `/wx` or the
WeChat login dialog on the channels page to sign in or change the account.

The private database lives at `data_root/channels/channels.sqlite3` with a `0700`
directory and `0600` files. Credential values are write-only: no API response,
log, trace or Agent context includes them. A connection exposes only an opaque
`credential_ref`. Editing without `credentials` keeps the old secret; sending a
new secret requires `replace_credentials: true`. Metadata rejects token/secret
fields, and C0 rejects URL-shaped credentials to avoid SSRF. Deleting a
connection requires explicit confirmation and removes configuration and
credentials, while delivery and inbound audit rows are retained.

API (same Web authentication as the rest of the Server; writes additionally
require the exact Origin or host bearer token):

```text
GET    /api/channels
GET    /api/channels/capabilities
GET    /api/channels/requests/{request_id}
GET    /api/channels/replies/{run_id}
GET    /api/channels/{platform}/connections
POST   /api/channels/{platform}/connections
PATCH  /api/channels/connections/{id}
DELETE /api/channels/connections/{id}
POST   /api/channels/connections/{id}/start|stop|reconnect
GET    /api/channels/connections/{id}/status
```

Create replies use `request_id` idempotency; PATCH/DELETE use
`expected_revision` plus `request_id`; DELETE also needs `confirm: true`. Reusing
a request ID with different input is a conflict, not a replay. The console keeps
the original submission identity after a lost response: retrying the same input
reuses its request ID, while changed input first reconciles the old receipt
through `GET /api/channels/requests/{request_id}` and only then receives a new
identity. Only the intent fingerprint and revision are stored; credentials are
never persisted in the browser, so a cross-reload retry requires re-entering the
same values. A completed create receipt opens the connection the Server already
created instead of creating a second one. Desired state is persisted and
restored after a Server restart with bounded exponential backoff
(no tight reconnect loop on bad credentials). Runtime state always comes from the
adapter's health report; configuration alone displays as `silent`. A delivery
left in `sending` by a process interruption becomes `unknown` after restart and
is never resent automatically. Inbound messages are deduplicated by
`(connection_id, external_message_id)` and bound to one Cairn Conversation per
external conversation.

Inbound work is authorized per connection before any Conversation or Run exists.
The `owner_identity` is always allowed, `allowed_senders` adds explicit platform
identities, and an empty list therefore behaves as owner-only. A stopped,
disabled or non-running connection rejects inbound work, and an identity allowed
on one connection or platform is not authorized on another. Inbound admission
uses a durable claim. A claim interrupted by cancellation or an ambiguous error
is stored as `unknown`; a redelivery reconciles the deterministic request ID
against an existing Run instead of creating a second side effect or staying
rejected forever. Configuration and credentials are still accepted without a
registered adapter.

The trusted connection record, never message text, decides the Run's Channel
principal. Only the owner receives personal memory, schedules and host tools; an
explicitly allowed sender reaches the same agent with an empty tool set and no
personal context, and an unknown sender is rejected before a Run exists.

### Telegram

A Telegram connection uses a Bot Token collected by the Web console and validated
with `getMe` before long polling starts. Only private text messages are accepted;
bot authors, the bot itself, groups and other update types are acknowledged and
ignored. The update offset is persisted only after local inbound admission, so a
crash between Telegram acknowledgement and the local write causes redelivery
that the deterministic inbound claim resolves to the same Run. A token can back
only one running connection in the process; a second one fails with
`telegram_token_in_use` instead of stealing updates. Network loss reconnects with
bounded backoff, a 409 marks the connection `error`, and 429 replies are retried
with the advertised delay. Replies are split below Telegram's 4096 UTF-16 limit,
sent as Markdown, and fall back to plain text when Telegram rejects the entities.

Every terminal Channel Run has a durable reply intent tied to its original
connection and external conversation. The coordinator sends through the normal
`ChannelManager.send` delivery audit, so a duplicate Run Settled event cannot
send twice. After restart, pending intents are delivered when the connection is
available and an interrupted in-flight send becomes `unknown`, never resent
automatically. A stopped or deleted connection records an explicit failure
reason instead of silently dropping the reply; `GET /api/channels/replies/{run_id}`
exposes its state, attempts and error.

The first connection remains the only WeChat protocol implementation; no
automatic credential migration is performed.

## Development

Use current code and behavioral tests to guide changes. Architecture and storage
can evolve with the product; this documentation describes the implementation.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts/ci
```

Run the regression tests for the code you change; the full offline suite is
`uv run pytest`. For parallel full acceptance with coverage and CI trigger
behavior, see [Tests](tests/README.md#feedback-cost).
[Operational scripts](scripts/README.md) have their own short index.
