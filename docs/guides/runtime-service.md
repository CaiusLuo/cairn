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

The Tasks page can submit work, display the same task_id/worktree, read sequenced
events and committed conversation, cancel, and continue a safely paused task.
TaskBoard is updated asynchronously from execution facts. Moving a board card
never starts or resumes execution. Each task keeps its worktree on completion or
pause. Continue first checks the exact clean DurableSession report, refreshes a
host-approved binding, then starts a new turn in the same checkpoint/worktree.
Unfinished or uncertain effects require separate host recovery, never replay.

The optional WeChat bridge uses the existing iLink receiver with separate private
state: add `--runtime-url http://127.0.0.1:8777 --runtime-token-file TOKEN_PATH` to
`cairn wechat serve`. `/status TASK_ID`, `/continue TASK_ID`, `/cancel TASK_ID` and
`/task PROMPT` use the same HTTP API; other text is submitted to that same Agent.
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
