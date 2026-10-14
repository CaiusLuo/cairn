# Experimental Cairn Pi TUI

A TypeScript terminal adapter using `@earendil-works/pi-tui@1.2.0`. Python owns
credentials, provider transactions, conversation, permissions and the existing
`run_turn`. The frontend uses `TuiMainScreen` with normal terminal scrollback.

## Install and launch

From the repository checkout, with Python 3.12, Node.js >=22.19 and pnpm:

```sh
uv sync --locked --all-groups
pnpm --dir ui/pi install --frozen-lockfile
pnpm --dir ui/pi build
uv run cairn tui
```

Configure providers as described in [configuration](../../docs/configuration.md).
Run from the project directory whose workspace/configuration Cairn should use.
The Python wheel contains the bridge and launcher, but not Node dependencies or
compiled frontend assets. With a wheel installation, build this frontend checkout
and run `cairn tui --ui-dir /absolute/path/to/cairn/ui/pi`.
The launcher preserves the working directory and gives the child its own Python
interpreter. It replaces itself with Node; there is no extra launcher process.

## Interaction

- Approve the displayed provider, endpoint, credential variable and model routing
  before credential resolution. Deny is the initial selection. After denial,
  `/provider` can choose another configured provider without activating the first.
- Enter submits; Shift+Enter inserts a newline on supporting terminals (Alt+Enter
  is also supported by Pi's editor). Editing and bracketed paste use Pi Editor.
- `/model` and `/provider` open native SelectList overlays. Up/down navigate,
  Enter selects, Escape cancels. Switching is rejected while busy.
- Tool cards show the action and running/completed/failed/denied status. `/tools`
  toggles bounded output for the cards, up to 4,000 characters each.
- Permission overlays offer Deny, Allow Once and Allow Session. Session grants
  belong to Python and reset only after a successful provider switch.
- Ctrl+C cancels an active operation, or clears the idle editor/selection.
  `/quit`, `/exit` or Ctrl+D closes after runtime cleanup. Repeated cancellation
  does not interrupt cleanup. The UI never force-kills the backend.
- Text streams into one Markdown component per attempt; a matching final message
  is not appended again. Abandoned attempts and cancelled turns stay labelled.

The default CLI and durable session commands retain their existing behavior.
This UI is in-memory only and does not create durable sessions or trace files.

## Local JSONL v1 contract

Launch the backend with `python -m cairn.terminal.bridge`. Stdout is JSONL only;
Python/provider diagnostics go to stderr, which the frontend drains without
showing potentially sensitive diagnostics. No HTTP listener or remote protocol
is involved. Only the local host frontend is trusted to convey human approvals.

Requests have `v: 1`, a positive, strictly increasing integer `id`, and `op`.
Lines are limited to 64 KiB, with exact fields and types; stale IDs and unknown
operations are rejected. Supported operations:

| op | Additional fields |
| --- | --- |
| `init`, `shutdown` | none |
| `prompt` | `text` |
| `provider`, `model` | `name` |
| `cancel` | target `request_id`, current `turn_id` (null before admission) |
| `approval` | `request_id`, `turn_id`, `approval_id`, `choice` |

The backend begins with `hello` and the packaged Cairn banner. `catalog` lists
configuration without resolving keys. `approval` identifies an exact pending
provider or capability request; it expires after 120 seconds. Wrong, duplicate,
expired, cancelled or disconnected approvals cannot create grants.
`approval_closed` dismisses that overlay. `ready` reports committed selection.

`event` carries the original Session `turn_id` and monotonically increasing
`sequence`, correlated with `request_id`. Only display events/fields are sent;
sequence gaps are expected. Tool output is bounded and raw tool/provider errors
are omitted. `ack` acknowledges a control message, never turn success.
`turn_settled` reports runtime outcome; `idle` is sent only after the operation
returns and owned cleanup is done. `error` uses fixed codes; `bye` confirms
orderly backend shutdown. EOF and SIGINT/SIGTERM request cancellation and wait
for owned execution to settle. The frontend checks version, request attribution,
and ordered turn event attribution; transport failure closes stdin for cleanup.

## Current integration limits and follow-up work

These are deferred integration constraints, not changes to core ownership:

1. **Reasoning stream:** `LLMStreamChunk` and runtime events carry answer text,
   tool-call deltas and finish metadata, but no separate reasoning delta. This
   UI therefore streams answers only. Add an explicit provider/core event
   contract before displaying reasoning; asynchronous execution alone cannot
   supply missing reasoning data.
2. **Synchronous approval API:** `SessionPermissionHandler` takes a synchronous
   callback. A small input thread validates control messages and wakes a
   condition; it never executes tools or mutates Agent state. Runtime execution
   stays on its asyncio thread. Future asynchronous approval should be designed
   at the existing permission owner, not through another Agent loop.
3. **Synchronous provider construction:** credential resolution and runtime/client
   construction are synchronous. Cancellation can wake a pending approval, but
   cannot preempt an already approved synchronous build. The UI stays responsive
   in its separate process; Python must finish construction before handling the
   queued stop. There is no forced timeout that could abandon cleanup.
4. **Tool attribution:** existing tool events lack a tool-call ID. Cards associate
   results with the current call under Cairn's existing sequential execution.
   Parallel tool execution would require explicit call attribution first.
5. **Distribution and diagnostics:** frontend assets are separately built, Unix
   signal handling is used, stderr is not displayed, and there is no trace UI.
   Tool stderr is omitted; failures show status, with bounded stdout available.
6. **Scrollback and retained display:** provisional text cannot reliably be
   retracted from terminal scrollback. Attempts are labelled incomplete or
   abandoned. Transcript components remain in memory for the session; there is
   no virtualization, durable replay or long-running session memory cap yet.

Approval grants authorize only current permission-policy capabilities. Neither
TypeScript nor protocol payloads can broaden the sandbox. Provider switching uses
`ProviderSession.select_provider`; model switching uses `select_model`, preserving
fallback order and the existing Agent conversation.

## Offline verification

```sh
uv run pytest tests/terminal/test_pi_bridge.py -q
pnpm --dir ui/pi check
pnpm --dir ui/pi build
pnpm --dir ui/pi test
```

The six Python subprocess regressions use `tests.support.pi_backend`, an offline
streaming provider and an in-process fake Bash execution method. They exercise
handshake/malformed requests, switching, ordered events, bounded output, stale
and denied approvals, busy rejection, cancellation/EOF cleanup, and sanitized
errors. They do not establish paid-provider behavior or real sandbox execution.
The two frontend tests render actual Pi components and check duplicate final text,
tool expansion, control-character filtering and incomplete output labels.

For an interactive offline smoke, set `CAIRN_TUI_PYTHON` to an executable wrapper
that runs this checkout's Python with `-m tests.support.pi_backend`, then run
`node /absolute/path/to/cairn/ui/pi/dist/main.js` from a temporary configured
workspace. The fixture accepts `tool`, `permission`, `slow`, and `fail` prompts;
ordinary prompts stream the selected model name. It makes no API requests.
