# WeChat

Cairn Server owns the iLink receiver. Start the [Server](runtime-service.md),
connect `cairn-cli --config /etc/cairn/config.toml` in the configured workspace,
and enter `/wx`. Web Settings offers the same login QR, verification, status,
trusted-sender and stop controls. API keys and WeChat credentials never go into
ordinary Server TOML, Git, prompts or service argv.

`/wx status` reports login/receiver state. `/wx verify CODE` completes requested
verification; `/wx allow SENDER` adds an explicitly trusted sender. `/wx stop`
stops the receiver and retains login. `/wx disconnect` clears the connection.
Closing CLI/Web leaves Server-owned work running. A persisted enabled receiver
with missing credentials refuses Server startup instead of silently losing WeChat.

| Message | Server behavior |
| --- | --- |
| Ordinary text | Open/continue this account and sender's shared conversation |
| `/task GOAL` | Submit an explicit development task |
| `/status [RUN_ID]` | Inspect a run in this sender's WeChat conversation |
| `/status TASK_ID`, `/task TASK_ID` | Inspect an existing WeChat development task |
| `/reply TASK_ID CONTENT` | Add a follow-up to the same task |
| `/history TASK_ID` | Read committed conversation |
| `/continue TASK_ID`, `/cancel TASK_ID` | Request shared continuation/cancellation |
| `/recovery TASK_ID` | Inspect retained recovery facts |
| `/schedule ...`, `/memory ...` | Use Server-owned services as the bound owner |

Commands that take a Task ID require a task created through WeChat with a
stored recipient and account matching the current sender and account. Missing
and inaccessible tasks return the same response. Host CLI and Web task controls
retain their existing access.

Stable message identities, sender/account checks and uncertain delivery receipts
remain active. A failed/busy conversation does not silently run another Agent;
reconciliation requires explicit host review. Rule changes still require reviewed
CLI/Web approval. Schedule reminders remain independent of the task worker.

State is `data_root/wechat`, with mode-0700 directories and private account files.
When an existing `state.json` is unreadable, the receiver stops processing and
preserves the file. Automatic admission resumes after valid state is restored
and the receiver is restarted.
On Debian, both the existing state-directory serve lock and a host-wide abstract
socket protect account identity. Copied credentials in a different data root
cannot start a second receiver for the same account. Kernel ownership releases
on process exit; never unlink a live lock to force startup.

The standalone WeChat unit, detached launcher entrypoints and separate bridge CLI
have been retired. Stop any existing old owner explicitly before importing its
state using the [Server migration flow](runtime-service.md#existing-data-and-explicit-import).
The new Server does not automatically convert pending standalone queue records
into shared tasks or replay uncertain effects.
