# Cairn

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
- In-process conversation state for user, assistant, and tool messages.
- LiteLLM-backed model calls, including tool-call parsing.
- A tool registry with Bash, bounded file reading, and guarded file editing.
- Permission decisions that allow selected read-only commands, deny selected
  commands, and ask before everything else.
- Runtime events for agent steps, tool calls, tool results, errors, completion,
  and step-limit termination.
- A Rich-powered interactive terminal interface.

The Bash tool executes commands in an OS sandbox after the permission handler
approves them. Its workspace is writable; commands cannot read other files in
the user's home directory or use the network. Cairn does not currently provide
persistent memory or background execution.

## Architecture

```text
src/
└── cairn/
    ├── core/          # Agent, loop, state, events, models, permissions
    ├── llm/           # LLM protocol and LiteLLM adapter
    ├── tools/         # Tool protocol, registry, and Bash tool
    ├── resources/     # Terminal banner
    ├── cli.py         # Interactive application wiring
    └── ui.py          # Rich output and permission prompts
tests/                 # Unit and behavior tests
```

Small protocols define the model-client, tool, event-handler, and
permission-handler boundaries. The CLI assembles the concrete LiteLLM client,
tool registry, Bash tool, and terminal handlers.

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
```

Start the CLI:

```bash
uv run cairn
```

A simple session looks like this:

```text
cairn> Inspect the files in this directory.
...agent and tool events appear here...
Cairn>
...model response appears here...
cairn> /exit
Goodbye! see you next time.
```

Use `/exit` or `/quit` to end the session. Cairn asks for confirmation before a
command that is not covered by its automatic allow or deny rules.

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

## Current Status / Roadmap

Cairn currently demonstrates a single-agent, single-process runtime with one
local tool and interactive permission checks. Near-term work can deepen the
runtime with more robust tool contracts, provider-backed integration tests,
permission policies, and end-to-end CLI validation.

Possible future work includes persistent memory, richer sandboxing, additional
tools, GitHub-oriented agent workflows, worktree isolation, multi-agent
coordination, and optional integrations such as LangGraph or LangSmith. These
capabilities are roadmap ideas and are **not implemented today**.
