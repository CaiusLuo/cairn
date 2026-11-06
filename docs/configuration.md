# Configuration

Model configuration is read from the target workspace, not the connecting CLI's
working directory. The shared service resolves credentials; its clients need
only the private Runtime token.

## Environment layout

When `.cairn/models.toml` is absent, shell variables and the workspace `.env`
select the provider. Shell values take precedence:

```dotenv
CAIRN_LLM_MODEL=openai/your-model
CAIRN_LLM_API_KEY=your-api-key
CAIRN_BASE_URL=https://api.example.com/v1
CAIRN_CONTEXT_MAX_TOKENS=98304
CAIRN_RESPONSE_MAX_TOKENS=8192
```

The context limit includes the response reserve; choose limits your model can
accept. Keep credential values in `.env` or the host environment, out of Git,
prompts and approval files.

## TOML layouts

A provider catalog in `.cairn/models.toml`:

```toml
context_reserve_percent = 10

[[providers]]
name = "example"
base_url = "https://api.example.com/v1"
api_key_env = "CAIRN_LLM_API_KEY"

[[providers.models]]
name = "primary"
model_ids = ["openai/your-model"]
```

The single-provider layout is also supported: put `base_url` and `api_key_env`
at the top level and use `[[models]]` for groups. Its provider name is `default`.
Do not mix the two layouts. Groups and ordered `model_ids` must be nonempty;
provider/group names and IDs within a group must be unique. Invalid existing
TOML stops startup instead of falling back to `.env`. Remote endpoints use HTTPS;
loopback HTTP is accepted for local models.

The first provider, first group and first model ID are selected at startup.
Local CLI `/model list`, `/model use NAME` and `/provider use NAME` manage
selection. `/model add GROUP ID`, `/model remove GROUP ID` and
`/model move GROUP ID POSITION` edit TOML while preserving comments and order;
restart to use persisted edits. They do not update a running shared service.

## Shared Runtime provider approval

Create a private JSON file outside the workspace and Worktree roots. It contains
these five facts, matching the selected configuration exactly, with no API key:

```json
{
  "name": "example",
  "endpoint": "https://api.example.com/v1",
  "group": "primary",
  "key_env": "CAIRN_LLM_API_KEY",
  "model_ids": ["openai/your-model"]
}
```

With `.env` only, the provider name is `default` and the group name is the
exact `CAIRN_LLM_MODEL` value (for example `openai/your-model`).
With single-provider TOML, use `default` and the first declared group name.
Supply the file as `--provider-approval FILE` to `cairn runtime serve`.
The host checks these facts before resolving the credential. `--offline` instead
uses a scripted local model, with no provider approval or paid API calls.

Runtime task budgets are separate from model context limits; see the
[Runtime guide](guides/runtime-service.md#budgets).

## Local extensions

The standalone CLI can discover project skills in `.cairn/skills/` and MCP server
configuration in `.cairn/mcp.toml`; `/skill` and `/mcp` expose their commands.
Skills are loaded on demand. MCP execution still requires host tool/network
permissions. The shared Runtime does not currently expose CLI skill/MCP selection.
Inspect `src/cairn/infra/skills.py` and `src/cairn/infra/mcp/` for supported layouts.
