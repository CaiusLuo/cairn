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

Cairn reads model configuration from one of three sources:

- **`.cairn/models.toml`, legacy layout** — a single provider:

  ```toml
  base_url = "https://example.com/v1"
  api_key_env = "BAILIAN_API_KEY"

  [[models]]
  name = "flash"
  model_ids = ["openai/model-a", "openai/model-b"]

  [[models]]
  name = "plus"
  model_ids = ["openai/model-c"]
  ```

- **`.cairn/models.toml`, catalog layout** — several named providers:

  ```toml
  [[providers]]
  name = "bailian"
  base_url = "https://example.com/v1"
  api_key_env = "BAILIAN_API_KEY"

  [[providers.models]]
  name = "flash"
  model_ids = ["openai/model-a"]

  [[providers]]
  name = "local"
  base_url = "http://localhost:8000/v1"
  api_key_env = "LOCAL_API_KEY"

  [[providers.models]]
  name = "default"
  model_ids = ["openai/local-model"]
  ```

- **`.env` only** (no TOML) — the single-model `CAIRN_*` settings above.

The layout is detected from the file: a file that declares `providers` uses the
catalog layout, and every other existing file must satisfy the legacy contract.
Mixed layouts, duplicate provider or group names, empty provider/group/model
lists and invalid endpoints or key variable names are rejected. A file that
exists but is invalid stops startup rather than falling back to `.env`; `.env`
settings are never migrated into TOML. Credential values are not resolved while
parsing, and no credential is read for a provider the session does not use.

Set each provider's key variable in your shell or `.env`, never in TOML. With
one provider Cairn selects it automatically; with several it asks which one to
use before any runtime exists. Startup then displays the selected provider, model
group and model IDs, and asks for approval showing the provider name, endpoint,
credential variable and a notice that prompts and conversation content are sent
to that endpoint. Credentials are resolved only after approval, and only for the
selected provider.

The first group is selected by default; multi-provider startup also offers an
explicit group choice. `/model use plus` switches groups without losing history.
Each completion starts at the selected group's first ID and, on eligible
failures, tries the remaining IDs followed by subsequent groups. The example
order is `model-a → model-b → model-c`; selecting `plus` starts at `model-c`.
Other errors stop immediately. Fallback never leaves the active provider and
does not change selection. `/model list` shows the loaded groups and recent
failures.

### Providers

`/provider` shows the active provider, its endpoint, credential variable and
current group. `/provider list` lists configured providers. `/provider use local`
switches provider after displaying the target endpoint, credential variable,
models and a warning that the conversation history is transferred, then asks for
approval. Only the target credential is resolved, and the switch commits only
after the target runtime is fully built, so denied approval, a missing credential
or a failed construction leaves the previous provider usable with its selection
and permission grants. A successful switch starts at the target's first model
group and resets session tool-permission grants; conversation history, workspace,
tools and traces are preserved. Commands run between turns only, so a provider
can never change mid-turn and model output is never executed as a command.

`/model add flash openai/model-d` appends an ID; `/model remove flash openai/model-d`
removes it. Both preserve comments and remaining order, change only local TOML,
and require a restart and provider approval to take effect. Groups must already
exist; duplicate IDs and removing a group's final ID are rejected. In the catalog
layout these edits apply to the active provider's group only, and every other
provider keeps its groups, order and formatting. Cairn does not migrate `.env` or
hot-reload configuration.

`/model move flash openai/model-b 1` moves an existing ID to the specified
1-based position within its group, consistent with `/model list`. Other IDs keep
their relative order. Unknown groups/IDs and invalid or out-of-range positions
are rejected; moving an ID to its current position does not rewrite the file.
The edit preserves string quoting, inline comments with their IDs, standalone
comments in place, and existing layout wherever supported. Unsupported formatting
is reported without writing. As with add/remove, restart and normal provider
approval are required; the running session keeps its loaded order.

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

## Source layout

```text
src/cairn/
├── core/          # Agent loop, state, context budgets, permissions
├── llm/           # Model configuration, selection, clients, token counting
├── tools/         # Bash and file tools
├── workspace/     # Filesystem roots and path boundaries
├── git/           # Local worktree creation, retention, and cleanup
├── observability/ # Trace recording and storage
├── terminal/      # CLI input, output, and slash commands
├── evals/         # Evaluation cases, checks, and runner
├── resources/     # Terminal banner
├── repository.py  # Git repository context
├── assembly.py    # Agent and tool assembly
├── config.py      # Environment and TOML loading
└── cli.py         # CLI runtime wiring
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

See [tests/README.md](tests/README.md) for test organization. To run the
[five-case coding smoke eval](examples/evals/run_coding_smoke.py) with a real
provider, configure the `CAIRN_LLM_*` and `CAIRN_BASE_URL` settings above, then run:

```bash
uv run python examples/evals/run_coding_smoke.py
```
