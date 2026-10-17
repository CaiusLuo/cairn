# Configuration

Cairn resolves model configuration from one of three layouts. It detects the
layout from the files you provide, and never guesses it from partial content.

## Layouts

### `.env` only

Used when `.cairn/models.toml` does not exist. The single provider is described
by `CAIRN_*` values:

```dotenv
CAIRN_LLM_MODEL=openai/your-model
CAIRN_LLM_API_KEY=your-api-key
CAIRN_BASE_URL=https://api.example.com/v1
CAIRN_CONTEXT_MAX_TOKENS=98304
CAIRN_RESPONSE_MAX_TOKENS=8192
```

Shell values take precedence over `.env`. The context budget includes the
response reserve, which is also sent as the provider's output-token limit. Choose
values that fit every model you intend to use. Older turns may be omitted from
requests while the full conversation stays in memory.

### `.cairn/models.toml`, legacy layout

A single provider with ordered model groups:

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

### `.cairn/models.toml`, catalog layout

Several named providers:

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

## Validation rules

A file that declares `providers` uses the catalog layout; every other existing
file must satisfy the legacy contract. Cairn rejects:

- mixed legacy and `[[providers]]` keys in one file;
- duplicate provider names, duplicate group names, or duplicate IDs in a group;
- empty provider, group or `model_ids` lists;
- the removed `model_id` field (use `model_ids = [...]`);
- `api_key_env` values that are not environment-variable names;
- `base_url` values with credentials, a query, a fragment, whitespace, or a
  scheme other than HTTPS (loopback HTTP is permitted for local endpoints).

A file that exists but is invalid stops startup rather than falling back to
`.env`, and `.env` settings are never migrated into TOML.

## Credentials

- Credential *values* live in your shell or `.env`, never in TOML.
- Parsing and editing never resolve credentials.
- Only the selected provider's credential is resolved, and only after you approve
  that provider for the session.
- Every configured provider's credential variable is withheld from Bash child
  environments, not just the active provider's.

## Session selection and switching

Startup uses the first configured provider, its first model group, and that
group's first model ID. Startup displays the selection and asks for approval,
showing the provider name, endpoint, credential variable, and a notice that
prompts and conversation content are sent to that endpoint.

Each completion starts at the selected group's first ID and, on eligible
failures, tries the remaining IDs followed by subsequent groups. Other errors
stop immediately. Fallback never leaves the active provider and does not change
selection.

See the [CLI command table](../README.md#cli-commands) for `/model` and
`/provider` usage.

A successful provider switch builds the target runtime
completely before committing, so denied approval, a missing credential, or a
construction failure leaves the previous provider usable with its selection and
permission grants. A switch resets session tool-permission grants and preserves
conversation history, workspace, tools and traces. Commands run between turns
only, so a provider never changes mid-turn.

## Editing model IDs

`/model add`, `/model remove` and `/model move` edit local TOML only:

- `/model add flash openai/model-d` appends an ID.
- `/model remove flash openai/model-d` removes one.
- `/model move flash openai/model-b 1` moves an ID to a 1-based position.

Edits preserve comments and remaining order, require a restart and normal
provider approval to take effect, and apply only to the active provider's group
in the catalog layout. Groups must already exist; duplicate IDs, removing a
group's final ID, unknown groups or IDs, and out-of-range positions are rejected.
Moving an ID to its current position does not rewrite the file. Unsupported
formatting is reported without writing. Cairn does not migrate `.env` or
hot-reload configuration.

## Where this lives in the code

| Responsibility | Module |
| --- | --- |
| Layout models and validation | `src/cairn/infra/config/schema.py` |
| TOML parsing and layout detection | `src/cairn/infra/config/loader.py` |
| Credential and budget resolution | `src/cairn/infra/config/secrets.py` |
| Transactional model-file edits | `src/cairn/infra/config/editor.py` |
| Stable import surface | `src/cairn/infra/config/__init__.py` |
