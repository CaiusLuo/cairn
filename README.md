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

Cairn is a lightweight agent harness built from first principles. It keeps model
calls, tool execution, permissions, and conversation state small enough to read,
test, and extend. This is an early-stage learning and engineering project.

- Asynchronous agent loop with context and step budgets.
- LiteLLM integration with session model selection.
- Local Bash execution, bounded file reading, and guarded file editing.
- Interactive Python CLI with model selection, permission prompts, and cancellation.
- Streaming answers and separate provisional thinking from `reasoning_content`.
- JSONL traces, [bounded Coding Task summaries](docs/guides/task-metrics.md),
  and headless coding evaluations.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No OS sandbox is
needed: the Bash tool runs commands locally, as you.

`uv` installs to `~/.local/bin` and that directory is often not on `PATH` on a
fresh Linux host, so `uv: command not found` usually means `PATH`, not a missing
install:

```bash
command -v uv || export PATH="$HOME/.local/bin:$PATH"
uv --version
```

```bash
uv sync --locked --all-groups
cp .env.example .env
```

`uv sync` needs network access the first time on a host with a cold cache. If it
fails offline, use a host or cache that already holds the locked wheels; the
command itself is not the problem.

### Git version for worktree workflows

The GitHub workflow and coding-task runner create Git worktrees, which need
**Git >= 2.40**. Check your version before that workflow:

```bash
git --version
```

Debian 12 (bookworm) ships **Git 2.39.5** by default, which is below that
requirement, so a stock Debian 12 needs a newer Git before the worktree and
Draft-PR workflows will behave. Plain `uv run cairn` and Durable Session do not
need worktrees.

### The interpreter's SQLite decides Durable mode

Durable mode judges the SQLite version the **running interpreter is actually
linked against**, not the version reported by a `sqlite3` command-line tool:

```bash
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

That value must be `3.51.3+`, `3.50.7+` or `3.44.6+`. A system without the
`sqlite3` CLI is fine; only the linked library matters. The same interpreter, or
a `uv venv` created from one, must be the one that runs Cairn.

### Entry points are not interchangeable

| Entry point | Command | Needs |
| --- | --- | --- |
| Standard CLI | `uv run cairn` | Python deps only |
| Durable Session | `uv run cairn session ...` | Python deps plus a qualified linked SQLite |

`python -m cairn` is not an entry point: the package has no `__main__`. Use
`uv run cairn` or `uv run python -m cairn.terminal.cli`.

Set your provider and budget in `.env`:

```dotenv
CAIRN_LLM_MODEL=openai/your-model
CAIRN_LLM_API_KEY=your-api-key
CAIRN_BASE_URL=https://api.example.com/v1
CAIRN_CONTEXT_MAX_TOKENS=131072
CAIRN_RESPONSE_MAX_TOKENS=8192
```

Both budget variables are optional; the defaults are `131072` and `8192`, sized
for the 128K window current hosted models offer. Lower them for a narrower
model — a budget above the model's window is rejected by the provider.

Shell values take precedence over `.env`. The context budget includes the
response reserve, which is also sent as the provider's output-token limit.
Older turns may be omitted from requests while the full conversation stays in
memory. The current turn and its tool results are never trimmed, so one very
large tool result can exceed the budget; the error then names the budget and
the attempt to raise it.

```bash
uv run cairn
```

This starts the Python CLI with the `cairn>` prompt. The CLI can display
streaming thinking separately from the answer when the provider
returns `reasoning_content`. Thinking is provisional and is not retained in
conversation history or traces. See [model streaming](docs/guides/model-streaming.md)
for supported providers and stream limits.

## Model configuration

Cairn reads model configuration from `.env` or `.cairn/models.toml`, in either a
legacy single-provider layout or a multi-provider catalog. Credentials live in
your shell or `.env`, never in TOML, and are resolved only for the provider you
approve.

See [Configuration](docs/configuration.md) for the supported layouts, validation
rules, credential handling, session selection and local `/model` edits.

## CLI commands

These commands are available at the `cairn>` prompt.

| Command | Purpose |
| --- | --- |
| `/model`, `/model list`, `/model use <name>` | Inspect or switch model groups |
| `/model add <group> <model-id>` | Append a local model ID; restart required |
| `/model remove <group> <model-id>` | Remove a local model ID; restart required |
| `/model move <group> <model-id> <position>` | Reorder an ID within its group (1-based); restart required |
| `/provider`, `/provider list` | Inspect providers and the active one |
| `/provider use <name>` | Switch provider after approval; resets session grants |
| `/trace`, `/trace <ID>`, `/trace list [N]` | Inspect saved traces |
| `/help [COMMAND]` | Full command usage, including trace management |
| `/exit`, `/quit` | End the session |

Traces are saved under `.cairn/traces/`. Default conversation state and approvals
last only for the current process. Opt into local durable conversation state with
`cairn session new`, inspect with `cairn session inspect SESSION_ID`, and reopen
with `cairn session reopen SESSION_ID`. Interrupted turns require explicit
`cairn session reconcile SESSION_ID`. See the [durable CLI guide](docs/guides/durable-cli.md)
for approval, storage and release limits; approvals are always fresh.

Bash commands run locally as your user and are **not** sandboxed: they can reach
the network and any file you can. A local shell is a host-level capability, so the
first command in a session asks for approval and, once trusted, later commands run
without repeating it. Configured API keys
are removed from Bash child environments, for every configured provider and not
just the active one; credential values never appear in prompts, traces or CLI
output. Accessible credential files can still be read by tools, so this is not
complete secret-file isolation.

## Skills

A skill is a directory holding `SKILL.md`: YAML frontmatter with a `name` and a
`description`, then a Markdown body. Only those two fields are read, so a skill
written for another harness loads unchanged.

Skills are advertised, not inlined. Each step shows one line per skill — its
name and description — and the body is absent until the agent calls the
`read_skill` tool for it. A library therefore costs a few hundred tokens until
something in it is used, instead of the full text on every request. Reading a
skill is an ordinary tool call, so it appears in traces with its arguments.

Cairn ships the `diagrams` skill, which says when a flow diagram is worth
drawing and when it is not. Skills are opt-in at assembly: the CLI enables the
packaged library, while evaluations and other callers start with none so a
measurement never inherits prompt text it did not ask for.

## Isolated workspaces

Local Git worktrees provide isolated workspaces for coding tasks, with automatic
cleanup and an option to retain work for later. The interactive CLI continues to
use your original workspace. Worktree support requires Git 2.40 or newer.

Git lifecycle commands use a restricted environment to keep configured Harness
credentials out of child processes and isolate system/global Git configuration.
Repository configuration and conditional includes remain checked. External Git
filters, including Git LFS content conversion, are not supported and cause a
clear error. This does not provide a process sandbox or prevent access to local
credential files.

Explicit [GitHub Issue workflows](docs/guides/github-workflow.md) use retained Worktrees
and deterministic verification before approved Draft PR publication.

[RepoGraph](docs/guides/repo-graph.md) provides bounded repository inventory and Python
structure queries through explicit, read-only snapshots.

## Architecture and flow

Four package areas, with dependencies pointing one way:

```text
        +---------------------------------------------------------+
        | terminal   CLI, slash commands, input and output        |
        +---------------------------------------------------------+
                                     |  one way only: a package may use
                                     |  any package below it; core uses
                                     |  nothing but core
                                     v
        +---------------------------------------------------------+
        | runtime    assembly, run_turn, model/tool execution     |
        +---------------------------------------------------------+
                                     |
                                     v
        +---------------------------------------------------------+
        | infra      providers, tools, git, sessions, traces      |
        +---------------------------------------------------------+
                                     |
                                     v
        +---------------------------------------------------------+
        | core       models, state, permissions, budgets          |
        +---------------------------------------------------------+
```

One Agent turn, owned by `runtime.loop.run_turn` from admission to settlement:

```text
        +---------------------------------------------------------+
        | terminal   read user input, show provisional text       |
        +---------------------------------------------------------+
                                     |
                                     v
        +---------------------------------------------------------+
        | run_turn   admit the owning Session, open the trace,    |
        |            add the user message                         |
        +---------------------------------------------------------+
                                     |
                                     v
        +---------------------------------------------------------+
        | step, at most budget.max_steps:                         |
        |   build system messages, tool schemas, memory           |
        |   model_executor.execute()      # candidate fallback    |
        |   add the assistant message; pending = tool calls       |
        +---------------------------------------------------------+  <--------+
               no tool calls         |             tool calls                 |
                     +----------------------------------+                     |
                     |                                  |                     |
                     v                                  v                     |
      +----------------------------+  +----------------------------------+    |
      | agent_finish: COMPLETED    |  | execute_tool_call, one call each |    |
      | return the content         |  |   preflight -> permission        |    |
      +----------------------------+  |   -> execute -> tool message     |    |
                     |                +----------------------------------+    |
                     |                                  +---------------------+
                     |
                     |                +--------------------------------------+
                     |                | failure or cancel (any step):        |
                     |                |   roll back the turn, or answer each |
                     |                |   pending call with TurnAborted      |
                     |                +--------------------------------------+
                     v                                    v
        +---------------------------------------------------------+
        | finally   close the turn trace, commit durability,      |
        |           settle the Session                            |
        +---------------------------------------------------------+
```

`run_turn` is the only Agent turn; task, workflow, review and evaluation code
runs bounded orchestration around it.

## Source layout

```text
src/cairn/
├── core/      # Agent and domain models, policies, and interfaces
├── runtime/   # Agent loop, assembly, and bounded task/workflow orchestration
├── infra/     # Providers, tools, filesystem/Git, GitHub, traces, and config
├── terminal/  # CLI entrypoint, input/output, and slash commands
├── resources/ # Packaged terminal assets
├── skills/    # Packaged skills, loaded on demand rather than up front
└── __init__.py
```

See [Architecture](docs/architecture.md) for turn and scheduling ownership and
guidance for placing new code, or the
[documentation index](docs/README.md) for every guide and experiment record.

## Development

This is the canonical local check set; other documents link here instead of
repeating it.

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

See [tests/README.md](tests/README.md) for test organization. To run the
[five-case coding smoke eval](evaluation/runners/run_coding_smoke.py) with a real
provider, configure TOML and credentials as above, then run:

```bash
uv run python evaluation/runners/run_coding_smoke.py --report .cairn/eval-reports/coding-smoke.json
```

The eval uses the same default provider and its first model ID, with normal TOML
provider approval. Without TOML it uses `.env`. It evaluates that one model;
interactive model switches and fallback selections are not carried into the eval.
