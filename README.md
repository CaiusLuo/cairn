<!-- banner:start -->
```text
      ___           ___                        ___           ___
     /  /\         /  /\           ___        /  /\         /  /\
    /  /::\       /  /::\         /__/\      /  /::\       /  /::|
   /  /:/\:\     /  /:/\:\        \__\:\    /  /:/\:\     /  /:|:|
  /  /:/  \:\   /  /::\ \:\       /  /::\  /  /::\ \:\   /  /:/|:|__
 /__/:/ \  \:\ /__/:/\:\_\:\   __/  /:/\/ /__/:/\:\_\:\ /__/:/ |:| /\
 \  \:\  \__\/ \__\/  \:\/:/  /__/\/:/~~  \__\/~|::\/:/ \__\/  |:|/:/
  \  \:\            \__\::/   \  \::/        |  |:|::/      |  |:/:/
   \  \:\           /  /:/     \  \:\        |  |:|\/       |__|::/
    \  \:\         /__/:/       \__\/        |__|:|~        /__/:/
     \__\/         \__\/                      \__\|         \__\/
```
<!-- banner:end -->

> A cairn marks the path for whoever comes next. So does a harness.

Cairn is a lightweight Agent Harness and runtime built from first principles.
The project is intentionally small: its purpose is to make the mechanics of an
agent loop—messages, model calls, tool execution, permissions, state, and
events—easy to read, test, and evolve without hiding them behind a large
framework.

Cairn is an early-stage learning and engineering project, not a production-ready
autonomous coding agent.

## Current capabilities

- An asynchronous agent loop with a configurable step limit.
- A configurable request context budget that omits complete older turns while
  retaining the full in-process history and current tool-call groups.
- A headless `EvalRunner` that runs one case per call, plus a five-case coding
  smoke example.
- In-process conversation state for user, assistant, and tool messages.
- LiteLLM-backed model calls, including tool-call parsing.
- A tool registry with Bash, bounded file reading, and guarded file editing.
- Sandbox-first permissions: normal local coding runs without approval, while
  network access requires an explicit capability approval.
- Runtime events for agent steps, tool calls, tool results, errors, completion,
  and step-limit termination.
- JSONL traces for model, permission, tool, and turn spans, with interactive
  commands for inspecting recorded traces.
- A Rich-powered interactive terminal interface.

The Bash tool executes every command inside an OS sandbox. On macOS, host reads
are broadly available, while writes are confined to the workspace, a safe host
`TMPDIR`, and the effective uv cache. On Linux, bubblewrap provides a narrower
mounted filesystem view. Network access is isolated by default; it is enabled
only for an execution with approved `NETWORK` capability. A session approval
applies to later explicit `NETWORK` requests in that Cairn process; a new
process starts without grants. A model-provided request flag is not approval.
The `sudo` check is a narrow, best-effort UX guardrail; the sandbox enforces the
filesystem and network boundary. Cairn does not currently provide persistent
memory or background execution.

## Architecture

```text
src/
└── cairn/
    ├── core/          # Agent runtime, context, budgets, state and permissions
    ├── llm/           # LLM protocol and LiteLLM adapter
    ├── tools/         # Tool protocol, registry, Bash and file tools
    ├── workspace/     # Shared filesystem root and path protection
    ├── repository.py  # Git repository context
    ├── evals/         # Eval models, checks, and runner
    ├── observability/ # Trace models, recording, and reading
    ├── terminal/      # Terminal input, output and permission prompts
    │   ├── commands/  # Interactive slash-command routing and handlers
    │   ├── input.py
    │   ├── output.py
    │   └── trace_output.py
    ├── resources/     # Terminal banner
    ├── assembly.py    # Reusable agent and tool assembly
    ├── config.py      # Environment configuration resolution and validation
    └── cli.py         # CLI entrypoint and interactive application wiring
tests/                 # Unit and behavior tests
```

Small protocols define the model-client, tool, event-handler, and
permission-handler boundaries. `build_agent()` takes a Workspace, model client,
and explicit permission, event, and trace dependencies, then wires the tools
to the same Workspace. The CLI reads environment configuration, resolves it
through `config.py`, and supplies terminal handlers; headless callers can reuse
configuration resolution and agent assembly without importing terminal code.
`repository.py` inspects Git state while `workspace/` owns filesystem path
boundaries. Trace recording and storage stay in `observability/`; their terminal
presentation lives in `terminal/trace_output.py`.

Directory ownership stays with the caller:

- CLI cwd is only the source used to construct Workspace.
- Workspace wraps an existing directory and normalizes its root; it does not
  create or clean up directories.
- EvalRunner creates and cleans up per-case temporary directories;
  Workspace wraps them without managing their lifecycle.

## Quick Start

Prerequisites:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- macOS `sandbox-exec` or Linux `bubblewrap`
- An API endpoint supported by LiteLLM

Install the project and development dependencies from the lock file:

```bash
uv sync --locked --all-groups
```

Create a local environment file:

```bash
cp .env.example .env
```

Set these values in `.env` for your provider:

```dotenv
CAIRN_LLM_MODEL=openai/your-model
CAIRN_LLM_API_KEY=your-api-key
CAIRN_BASE_URL=https://api.example.com/v1
CAIRN_CONTEXT_MAX_TOKENS=98304
CAIRN_RESPONSE_MAX_TOKENS=8192
```

The context limit includes system and repository context, conversation messages,
tool schemas, and the reserved response allowance; a request is sent only while
counted input plus the response reserve fits within `CAIRN_CONTEXT_MAX_TOKENS`.
The `.env.example` values above are an intentionally conservative 98,304-token
total budget with 8,192 reserved for the response, leaving headroom below a
128k-model context window. When both settings are unset the CLI falls back to the
built-in defaults, 32,768 counted tokens with 4,096 reserved for the response. The
response limit is also sent to LiteLLM as `max_tokens`. How that limit is
enforced depends on the selected model and provider. Set both values for your
model; the response allowance must be positive and smaller than the context
limit. Shell values take precedence over `.env` without being copied into
child-process environments.

The CLI counts requests with a `LiteLLMTokenCounter`, which uses the tokenizer
LiteLLM resolves for `CAIRN_LLM_MODEL` and labels the result exact. When LiteLLM
cannot count, it falls back to an offline estimate of serialized UTF-8 byte
lengths plus framing overhead, labelled as an estimate, so an approximation is
never presented as provider usage. That estimate overestimates prose but can
underestimate code, hexadecimal and base64-like content, so prefer the
tokenizer-backed count and treat an estimate as a signal, not a guarantee.
Headless callers can inject a `ContextBuilder` with a `ContextBudget` and a
`TokenCounter` into `Agent` or `build_agent()`; custom counters label their
result as exact or estimated using `TokenCount`. When using a custom model
client, configure its output limit to match the reserved response allowance.

Older turns are omitted only from the request view, including whole assistant
tool-call/result groups, and Cairn reports the omission. The current turn is never
trimmed. If its required content cannot fit, Cairn raises `ContextBudgetExceeded`
before another model request, naming the never-trimmed components in the error. A
malformed older tool-call group is omitted the same way instead of failing every
later turn; a malformed current turn raises `ConversationHistoryError`. Trace
attributes record counted input before and after trimming, omission counts, the
budget and reserve, and whether the count is estimated; provider usage remains
separate. A model failure or cancellation without provider usage displays
`tokens: input unknown, output unknown`.

Start the CLI:

```bash
uv run cairn
```

A simple session looks like this:

<!-- cli-banner:start -->
```text
      ___           ___                        ___           ___
     /  /\         /  /\           ___        /  /\         /  /\
    /  /::\       /  /::\         /__/\      /  /::\       /  /::|
   /  /:/\:\     /  /:/\:\        \__\:\    /  /:/\:\     /  /:|:|
  /  /:/  \:\   /  /::\ \:\       /  /::\  /  /::\ \:\   /  /:/|:|__
 /__/:/ \  \:\ /__/:/\:\_\:\   __/  /:/\/ /__/:/\:\_\:\ /__/:/ |:| /\
 \  \:\  \__\/ \__\/  \:\/:/  /__/\/:/~~  \__\/~|::\/:/ \__\/  |:|/:/
  \  \:\            \__\::/   \  \::/        |  |:|::/      |  |:/:/
   \  \:\           /  /:/     \  \:\        |  |:|\/       |__|::/
    \  \:\         /__/:/       \__\/        |__|:|~        /__/:/
     \__\/         \__\/                      \__\|         \__\/


A cairn marks the path for whoever comes next.
So does a harness. ✨

cairn> Inspect the files in this directory.
...agent and tool events appear here...
Cairn>
...model response appears here...
cairn> /exit
Goodbye! see you next time.
```
<!-- cli-banner:end -->

Use `/exit` or `/quit` to end the session. Normal local operations within the
sandbox need no approval; an explicit request for extra `NETWORK` capability
requires approval, which can apply to later network requests in that session.

Each turn records trace spans under `.cairn/traces/`. A trace is one append-only
JSONL file named by its trace ID. Listing metadata is read from the final root
span at the tail of that file. These commands inspect or manage stored traces
without calling the model:

```text
/trace
/trace TRACE_ID
/trace list
/trace list N
/trace count
/trace del TRACE_ID
/trace del --tail N
```

`/trace list` shows the latest 10 completed traces, newest first. Use
`/trace list N` to choose the number of traces to show, where `1 <= N <= 100`.
`/trace count` prints how many trace files are stored, without parsing them.
`/trace del TRACE_ID` deletes one trace and echoes the deleted IDs and count;
`/trace del --tail N` deletes the N oldest traces, i.e. the end of `/trace list`.
A trace ID may be a full ID or a unique prefix; an ambiguous prefix reports the
matching candidates and deletes nothing. Batch deletion skips traces whose final
root span cannot be read, warns about each one, and never aborts the whole batch.
Deleting is permanent and never asks for confirmation.

Use `/help` to list all interactive commands.

## Coding smoke evals

Run the five-case coding smoke eval from the repository root:

```bash
uv run python examples/evals/run_coding_smoke.py
```

It uses the same `CAIRN_LLM_MODEL`, `CAIRN_LLM_API_KEY`, and `CAIRN_BASE_URL`
configuration as the CLI. Shell environment values take precedence over `.env`;
`.env` values are not injected into child processes. The cases check single-file
bug fixes, multi-file refactoring, helper extraction, minimal changes, and
correct no-op behavior. Each run allows 20 steps and 120 seconds, with 2 seconds
per check. The baseline is noninteractive and has no `NETWORK` grants; provider
calls use the normal host credentials.

Traces are saved under `.cairn/eval-traces`, outside temporary workspaces. FAIL
and ERROR lines include a trace ID when available. Exit codes are 0 when all
cases pass, 1 for any FAIL or ERROR, and 2 for missing configuration. This is a
simple one-run signal, not a benchmark, and text checks do not establish general
Python semantics. A no-op PASS means only that the file stayed unchanged; inspect
its trace to confirm the agent examined the file.

## Development

Run the complete local quality suite:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

The coverage command runs the complete pytest suite, measures branch coverage,
and enforces the 80% project gate. For a faster local test run without coverage,
use `uv run pytest`. GitHub Actions runs the quality suite from a clean checkout.

To apply the repository formatter locally:

```bash
uv run ruff format .
```

To build the source distribution and wheel locally:

```bash
uv build
```

## Current limits

Cairn runs in a single process and keeps conversation state in memory. Its
`EvalRunner` runs one case per call; the coding smoke example runs five fixed
cases.

Cairn does not provide worktree management, task orchestration, GitHub
publishing, reviewer workflows, a RepoGraph, persistent memory, background
execution, or automatic merging.
