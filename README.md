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

For ordered model groups under one provider, create `.cairn/models.toml` in your
working directory:

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

Set `BAILIAN_API_KEY` in your shell or `.env`, never in TOML. Cairn asks for
provider approval at startup. Without TOML, it uses the single-model `.env`
settings; invalid TOML stops startup. Both files are gitignored.

The first group is selected by default. `/model use plus` switches groups without
losing history. Each completion starts at the selected group's first ID and,
on eligible failures, tries the remaining IDs followed by subsequent groups.
The example order is `model-a → model-b → model-c`; selecting `plus` starts at
`model-c`. Other errors stop immediately. Fallback does not change selection.
`/model list` shows the loaded groups and recent failures.

`/model add flash openai/model-d` appends an ID; `/model remove flash openai/model-d`
removes it. Both preserve comments and remaining order, change only local TOML,
and require a restart and provider approval to take effect. Groups must already
exist; duplicate IDs and removing a group's final ID are rejected. Cairn does
not migrate `.env`, reload configuration or support multiple providers.

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
| `/trace`, `/trace <ID>`, `/trace list [N]` | Inspect saved traces |
| `/help [COMMAND]` | Full command usage, including trace management |
| `/exit`, `/quit` | End the session |

Traces are saved under `.cairn/traces/`. Conversation state and approvals last
only for the current process.

Bash commands run inside an OS sandbox with network access denied by default;
extra network access requires explicit approval. Filesystem boundaries differ
by platform, and host reads are broadly available on macOS. Configured API keys
are removed from Bash child environments, but accessible credential files can
still be read by tools.

## Source layout

```text
src/cairn/
├── core/          # Agent loop, state, context budgets, permissions
├── llm/           # Model configuration, selection, clients, token counting
├── tools/         # Bash and file tools
├── workspace/     # Filesystem roots and path boundaries
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
