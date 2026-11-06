# Shared Runtime Service (experimental)

The opt-in Runtime owns task admission, a bounded queue, one worker, cancellation
and the ordered public event history. Entry adapters submit or read facts; they
never start their own execution for a Runtime task.

`runtime.task_service.TaskService` exposes submit, inspect, list_tasks, events,
follow, continue_task and cancel. `infra.runtime_catalog.RuntimeCatalogue` holds
host-private admission/worker facts under an exclusive process lease. A damaged
catalogue fails closed. After restart an observed running task becomes
recovery_required, never automatically replayed. Only safely paused tasks may
continue; queued tasks can be recovered without pretending they already ran.

The TaskBoard is a presentation projection, not an execution command. Durable
turn/checkpoint and effect authority remain with SessionStore/DurableSession and
the existing tool boundary; catalogue records cannot grant execution permission.

This is an experiment on feat/langgraph-spike. It must use a separate state
folder and endpoint from existing online WeChat/Web services.

## Run the isolated service

Build the existing UI in this checkout with `cd web && pnpm install --frozen-lockfile && pnpm build`.
Use host-private state outside both the source repository and the worktree parent:

```sh
cairn runtime serve --workspace /srv/project --state-dir /var/lib/cairn/runtime-test --worktrees /var/lib/cairn/task-worktrees --offline --port 8777
cairn runtime submit "Create the acceptance artifact" --token-file /var/lib/cairn/runtime-test/runtime.token
cairn runtime list --token-file /var/lib/cairn/runtime-test/runtime.token
cairn runtime inspect TASK_ID --token-file /var/lib/cairn/runtime-test/runtime.token
cairn runtime follow TASK_ID --token-file /var/lib/cairn/runtime-test/runtime.token
cairn runtime continue TASK_ID --token-file /var/lib/cairn/runtime-test/runtime.token
```

The server listens only on loopback. Open `http://127.0.0.1:8777/tasks.html`
through an SSH tunnel from the viewing machine. Login username is `cairn`; the
password is the private runtime.token file. Do not expose that token to an Agent.
The `--offline` acceptance model uses the real write_file tool to create
runtime-result.txt in an isolated Git worktree; it makes no provider request.

The Tasks page can start conversations or explicit development tasks, send follow-up messages to the selected task, display the same task_id/worktree, read sequenced
events and committed conversation, cancel, and continue a safely paused task.
TaskBoard is updated asynchronously from execution facts. Moving a board card
never starts or resumes execution. A development task keeps its worktree on completion or
pause; ordinary conversations do not allocate one. Continue first checks the exact clean DurableSession report, refreshes a
host-approved binding, then starts a new turn in the same checkpoint/worktree.
Unfinished or uncertain effects require separate host recovery, never replay.

The optional WeChat bridge uses the existing iLink receiver with separate private
state: add `--runtime-url http://127.0.0.1:8777 --runtime-token-file TOKEN_PATH` to
`cairn wechat serve`. `/status TASK_ID`, `/continue TASK_ID`, `/cancel TASK_ID` and
`/task PROMPT` use the same HTTP API; ordinary text opens or continues a conversation scoped to that bot and trusted sender. `/reply TASK_ID CONTENT` continues a chosen shared task.
Sender/context/stable-ID admission precedes forwarding. The source message ID
is an idempotency key; a lost acknowledgement cannot cause duplicate execution.
The bridge does not invoke its local classifier/Agent or run the old scheduler.
It refuses pending legacy records instead of treating them as runtime tasks.

Shared endpoints: GET/POST `/api/runtime/tasks`, GET `/api/runtime/tasks/ID`,
GET `/api/runtime/tasks/ID/events?after=SEQ`, GET `/api/runtime/tasks/ID/history`,
POST `/api/runtime/tasks/ID/continue` and POST `/api/runtime/tasks/ID/cancel`.
They use the Web host's cookie/Origin protection or the private bearer token.
Event pages are bounded and reconnect with the returned cursor; reads never run
a tool. RuntimeCatalogue owns task admission/lifecycle, DurableSession owns turn
receipts/fences; CodingTaskRunner's conversation-only mode avoids introducing a
second TaskLedger lifecycle for these service tasks.


## Approved providers and one budget policy

For a real provider, replace `--offline` with `--provider-approval /private/provider.json`.
This file contains exactly `name`, `endpoint`, `group`, `key_env`, and the ordered
`model_ids` array matching `.cairn/models.toml` or the existing environment layout.
The same approval loader as `cairn task run` checks all five facts **before** resolving
credentials. The resulting existing ProviderRuntime supplies its model executor,
context builder and matching fallback runtimes. No new provider client exists.
Only `--offline` uses the scripted acceptance model. Real provider execution is
opt-in and is not exercised by offline acceptance.

Host policy files and state must live outside source/worktree writable roots.
`--budget-config /private/runtime-budget.toml` accepts exactly one `[budget]` table:

```toml
[budget]
max_steps = 1000
max_seconds = 900
max_tokens = 200000
max_stagnant_steps = 12
# Optional cost ceiling, using host-selected upper rates for ALL approved models:
# max_cost = 2.0
# input_cost_per_million = 5.0
# output_cost_per_million = 15.0
```

All Runtime entries use this server policy; clients cannot override it. Default
policy is 500 steps, 900 seconds and 12 repeated unchanged tool batches. The last
hard validation ceiling is 100000 steps. The existing durable journal has a
256-effect fence capacity per turn; reaching it now pauses cleanly before a new
effect, preserving receipts and allowing the next explicitly continued turn. Native legacy CLI/headless defaults also
use the shared 500-step constant; the former 150-step default and extra WeChat
20-step clamp are removed. Explicit host-selected values remain supported.
Time controls the admitted Agent turn, including owned model/tool cleanup; queue
waiting and worktree preparation are reported separately in total elapsed time.
Token/cost stops are checked before another request or tool: one in-flight response
can exceed the configured threshold. This is not a provider billing reservation.
Unknown usage/cost stays null; token/cost-controlled execution pauses rather than
pretending an unmeasured request was free. Cost is a rate-based estimate, not an invoice.

A paused task retains phase results, original conversation and worktree. Explicit
continue authorizes a fresh budget window; metrics retain cumulative requests,
steps, tool attempts, tokens, elapsed time, stop reason and per-attempt limits.
A cancelled tool, an unknown effect or an unfinished turn instead requires review:

```sh
cairn runtime recovery TASK_ID --token-file TOKEN_PATH > /private/recovery.json
# Inspect the reported fences, committed history and actual worktree first.
cairn runtime reconcile TASK_ID --report-file /private/recovery.json --effects-reviewed --note "Verified surviving changes; summarize without rewriting" --token-file TOKEN_PATH
cairn runtime continue TASK_ID --token-file TOKEN_PATH
```

Recovery uses fresh catalogue sequence + SessionStore revision CAS and the existing
`abandon_without_replay` protocol. Reconcile itself runs no model/tool. Web provides
the same report, explicit effects checkbox and required note, then a separate
Continue button. WeChat `/recovery TASK_ID` reads the report; confirmation stays
with the authenticated host CLI/Web. A crash before durable session admission
requires host inspection rather than guessing an original session.

`cairn runtime history TASK_ID` and WeChat `/history TASK_ID` read the shared committed
history. The WebSocket `/api/runtime/tasks/ID/stream?after=SEQ` replays sequenced
facts and streams provisional answer text. Transient text is not checkpoint truth
or persisted per token. Subscribers have bounded queues; slow consumers reconnect
with their cursor. Split declared secrets are redacted before presentation;
failed-stream suffixes are discarded. Protected Runtime state and credentials
are blocked by file tools in the Agent task context. `--allow-shell` explicitly
opts into the existing project sandbox shell (including its network capability);
isolation unavailable never falls back to host shell. No client request can grant it.

## Replaced and retained paths

LangGraph now sequences the existing `run_turn` model/tool nodes; the previous
hand-written `for` loop and the separate experiment loop are removed. The spike
facade delegates to that same owner. Runtime-mode WeChat bypasses its old
classifier, task worker and scheduler; CLI/Web do not construct their own Agent.
Provider approval and telemetry redaction are shared helpers rather than copied
implementations. The standalone legacy entry remains available during opt-in
acceptance; after migration of online users, its private classifier/worker route
can be removed. There is one reusable Agent turn implementation today.

Permissions/preflight, credential filtering, context admission/compaction, traces,
DurableSession receipts/fences, SQLite qualification and WorktreeProvider remain.
LangGraph supplies orchestration, not authority, persistence or safe replay.
The first service runs one task at a time with a bounded admission queue. Status
and event subscribers remain independent of blocked model awaits; parallel task
workers and scheduler migration are outside this experimental slice.

## Connect the native interactive CLI

The existing terminal can connect without constructing a local Agent, loading
provider credentials, opening a SessionStore writer or starting local schedules:

```sh
cairn --runtime-url http://127.0.0.1:8777 --runtime-token-file /private/runtime/runtime.token
cairn --runtime-url http://127.0.0.1:8777 --runtime-token-file /private/runtime/runtime.token --task-id TASK_ID
```

The multiline composer, transcript and tool activity display are reused. `/use ID`
attaches to an existing task and shows committed replies; `/history`, `/status`,
`/continue`, `/cancel`, `/new` and `/exit` operate on shared service facts. New
messages after a completed turn use the original task ID and committed history.
A development task also retains its Session and Worktree.
The message API uses a fresh task sequence and a stable request ID: concurrent
stale inputs are rejected, and an acknowledgement lost after admission is retried
without executing another turn. Clients must refresh history before retrying a
stale write. Model/permission configuration stays with the host service.

Ctrl+C on the running terminal surface calls the server's cancel operation.
Exiting/detaching only closes the client; it never takes ownership of the remote
Agent. Input typed while a task runs waits for the next settled turn. Unfinished
or unknown effects continue to require the same explicit recovery review.
`cairn` without Runtime options retains its explicit local execution mode during
this opt-in migration. The connection mode never falls back to local execution.


## Conversation and development are one execution path

Interactive CLI and Web default to conversation mode. Plain WeChat messages reuse
one server conversation per bot/sender; `/task GOAL` explicitly creates a new
development task. `cairn runtime submit` remains an explicit development command
for batch callers. The HTTP submit body accepts `mode: conversation|development`;
clients cannot use it to grant shell/network access. `/develop GOAL` in the
interactive CLI is the equivalent explicit development shortcut.

Ordinary conversations use the existing LangGraph turn, model executor, durable
Session and budget. They advertise only read_file, glob, grep and the narrow
begin_development control tool. They do not scan repository context or create a
Git Worktree. The model can answer, inspect read-only files, or request development
through that tool; there is no keyword or separate classification model.

After all tools in the conversation group settle, the host emits a handoff receipt
and commits that conversation turn. TaskService then advances the same task into
its development phase, within the same queue job. WorktreeProvider creates the
isolated worktree and the full development tool registry is bound with fresh host
permissions. No source-workspace mutation tool is available before this boundary.
The old binding is never silently moved: a new workspace gets a new Session UUID,
and the original checkpoint receipt remains in history_origins. Committed earlier
conversation is supplied as lower-trust historical context, with every receipt
checked before reuse. The shared history API includes both phases. Subsequent
turns keep the development Session and Worktree, including across service restart.
Historical file observations must be revalidated in the new workspace before edits.

The automatic handoff does not replenish time/token/cost/step budgets. Metrics
retain the current explicit user request's budget window as well as cumulative
usage. If the conversation consumed the window, the task pauses before creating
the worktree; explicit continue grants another window. New user input can replace
a pending development request before a worktree is allocated. Unknown effects
still block new input and require explicit host reconciliation in either mode.

The shared terminal keeps input typed during execution for the next settled turn;
it does not claim to steer an already running model request. Tool progress is
shown live from durable events, and the final reply is displayed after settlement.
Web retains its provisional token stream. When a WeChat conversation is busy, a
new natural-language input is explicitly reported as not executed and must be
resent after completion; status/cancel remain available. No client starts a local
Agent as a fallback.

The offline model validates conversation transport and explicit development
commands. It is scripted and does not classify natural-language development
requests; adaptive tool choice is covered by offline model doubles and remains a
real-provider acceptance item. RuntimeCatalogue's full-JSON event write amplification
is unchanged by this slice and remains a separate persistence optimization.
