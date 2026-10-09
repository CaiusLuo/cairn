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

Set `BAILIAN_API_KEY` in your shell or `.env`; keep the key out of TOML. Cairn
uses the first configured group by default. Group names must be unique, and each
`model_ids` array must contain one or more nonblank strings without duplicates.
The old `model_id` field is rejected; replace it with a `model_ids` array.
Without TOML, Cairn uses the single-model settings above as a one-ID group;
invalid TOML stops startup. Cairn does not generate, migrate or rewrite TOML.

Each TOML-based startup requires explicit terminal approval of the provider and
credential variable, defaulting to no. Endpoints require HTTPS except for
loopback HTTP services. Review the endpoint and model IDs before approving.

Use `/model use plus` to select a group for the current session without losing
history. Each completion starts at that group's first ID. An eligible fallback
error tries the next ID in the same group, then the next group only after all
IDs in the current group fail. The example order is `model-a -> model-b ->
model-c`; selecting `plus` starts directly at `model-c`. Other errors stop
immediately. Fallback success does not change the selected group.

Each attempt uses its concrete model's client and token counter and checks the
context budget before sending a request. Budget settings, endpoint and API key
remain shared. `/model list` shows groups, ordered IDs and the most recent
failure per ID. Failures are session diagnostics only and never skip candidates.
Provider selection, cross-provider fallback and persistent config editing are
not supported. Keep `.env` and `.cairn/models.toml` local; both are gitignored.

## CLI commands

| Command | Purpose |
| --- | --- |
| `/model`, `/model list`, `/model use <name>` | Inspect or switch model groups |
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
