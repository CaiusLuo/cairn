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
- Sandboxed Bash, bounded file reading, and guarded file editing.
- Interactive CLI, JSONL traces, and headless coding evaluations.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and macOS `sandbox-exec`
or Linux `bubblewrap`.

```bash
uv sync --locked --all-groups
cp .env.example .env
```

Set your provider and budget in `.env`:

```dotenv
CAIRN_LLM_MODEL=openai/your-model
CAIRN_LLM_API_KEY=your-api-key
CAIRN_BASE_URL=https://api.example.com/v1
CAIRN_CONTEXT_MAX_TOKENS=98304
CAIRN_RESPONSE_MAX_TOKENS=8192
```

Shell values take precedence over `.env`. The context budget includes the
response reserve, which is also sent as the provider's output-token limit.
Choose values that fit every model you intend to use. Older turns may be omitted
from requests while the full conversation stays in memory.

```bash
uv run cairn
```

## Model configuration

Cairn reads model configuration from `.env` or `.cairn/models.toml`, in either a
legacy single-provider layout or a multi-provider catalog. Credentials live in
your shell or `.env`, never in TOML, and are resolved only for the provider you
approve.

See [Configuration](docs/configuration.md) for the supported layouts, validation
rules, credential handling, session selection and local `/model` edits.

## CLI commands

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

Traces are saved under `.cairn/traces/`. Conversation state and approvals last
only for the current process.

Bash commands run inside an OS sandbox with network access denied by default;
extra network access requires explicit approval. Filesystem boundaries differ
by platform, and host reads are broadly available on macOS. Configured API keys
are removed from Bash child environments, for every configured provider and not
just the active one; credential values never appear in prompts, traces or CLI
output. Accessible credential files can still be read by tools, so this is not
complete secret-file isolation.

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

## Source layout

```text
src/cairn/
├── core/      # Agent and domain models, policies, and interfaces
├── runtime/   # Agent loop, assembly, and bounded task/workflow orchestration
├── infra/     # Providers, tools, filesystem/Git, GitHub, traces, and config
├── terminal/  # CLI entrypoint, input/output, and slash commands
├── resources/ # Packaged terminal assets
└── __init__.py
```

See [Architecture](docs/architecture.md) for dependency direction, turn and
scheduling ownership, and guidance for placing new code, or the
[documentation index](docs/README.md) for every guide and experiment record.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

See [tests/README.md](tests/README.md) for test organization. To run the
[five-case coding smoke eval](examples/evals/run_coding_smoke.py) with a real
provider, configure TOML and credentials as above, then run:

```bash
uv run python examples/evals/run_coding_smoke.py --report .cairn/eval-reports/coding-smoke.json
```

The eval uses the same default provider and its first model ID, with normal TOML
provider approval. Without TOML it uses `.env`. It evaluates that one model;
interactive model switches and fallback selections are not carried into the eval.
