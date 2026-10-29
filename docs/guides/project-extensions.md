# Project skills and MCP servers (`.cairn`)

Cairn can load skills and external tools declared under a project's `.cairn`
directory. Both are opt-in and configurable: nothing is installed or started
until the operator selects it, and a host capability grant is required before an
MCP process or connection exists. The same discovery/selection path is used by
the interactive terminal and by the server entry (`cairn task run`), so a future
IM integration can reuse the existing runtime instead of a second loop.

## Directory layout

```
.cairn/
  skills/
    reporting/
      SKILL.md
      resources/report-template.md
  mcp.toml
  models.toml        # provider configuration (separate; see configuration.md)
```

A missing directory is not an error: a project simply has no skills or MCP
servers. Malformed configuration raises instead of falling back silently.

## Skills

`SKILL.md` uses ordinary frontmatter; Cairn reads only `name` and `description`
and ignores other common fields, so a skill written for another harness loads
unchanged:

```markdown
---
name: reporting
description: Write a status report for a stakeholder.
---

When asked for a status report, ... (body is loaded only when used)
```

- Discovery is layered: a project skill shadows a builtin skill with the same
  name (deterministically, first directory wins inside one layer); shadowed and
  duplicate names are reported by the catalog.
- The body is **not** advertised every turn: only name + description are listed,
  and the body arrives when the skill is read or explicitly selected.
- Relative resource files are read with `read_skill_resource` and are confined to
  that skill's directory (`..`/absolute paths are rejected).
- Selecting a skill is task context the operator chose. In the terminal type
  `/skill`, pick with Up/Down (typing filters; more than six rows scroll), press
  Enter to insert a plain-text token like `[skill:reporting]` into the still
  editable composer, then type the task and press Enter to send. Multiple
  selections can be added. The body is delivered as a **user** message for that
  turn — never as system policy, and never granting permission.

Server entry:

```sh
cairn task run --workspace /srv/proj --prompt "..." --trust-workspace \
  --skill reporting
```

Unknown skills are rejected before any agent turn.

## MCP servers

Declare servers in `.cairn/mcp.toml`. `env_keys` names host environment variables
the child may receive; **values are never stored in the repository**.

```toml
[[servers]]
name = "local-tools"
transport = "stdio"
command = "python"
args = ["-m", "my_pkg.mcp_server"]
env_keys = ["MY_TOOL_TOKEN"]      # names only; values resolved from the host env

# [[servers]]
# name = "remote-tools"
# transport = "streamable_http"   # listed, but see current support below
# url = "https://example.test/mcp"
```

Terminal `/mcp` lists configured servers without starting them; selecting one
inserts `[mcp:<name>]`. Server entry:

```sh
cairn task run ... --mcp local-tools --allow-mcp --allow-shell
```

### Authorization and lifecycle

| Fact | Grants |
| --- | --- |
| `--allow-mcp` / interactive approval | `MCP_SERVER` (start/connect) and `MCP_TOOL` (invoke a discovered tool) |
| stdio transport | also `LOCAL_SHELL` (a host process is started) |
| `streamable_http` | also `NETWORK` |
| `--allow-shell`, `--allow-network` | the matching host capabilities for the server entry |

Authorization completes **before the process is spawned or the connection is
opened**; the offline `started.marker` regression proves an unauthorized
activation never starts the server. Discovered tools are namespaced
`mcp__<server>__<tool>`, `requires_authorization` is set, and the runtime forces
an ASK for them so a dynamic name can never inherit the baseline ALLOW. Without a
grant the call is denied and the server is not invoked. Only the selected
servers' tools are exposed for that turn/task; they are removed afterwards.
Timeouts, cancellation and errors close the process group/connection; child
environments contain only a safe baseline plus the configured `env_keys`;
configured credential values are redacted from tool output.

## Current support and limits

- **stdio**: implemented in-tree with a small JSON-RPC client (initialize,
  tools/list, tools/call).
- **streamable_http**: declared and listed, but not connected: the official MCP
  Python SDK could not be installed in this environment (its dependency set —
  `httpx2`, `mcp-types`, `cryptography` — did not resolve within the build
  budget). Activation reports a clear unsupported error before any network
  connection; stdio remains fully supported.
- No automatic installation or execution of unknown third-party skills/servers:
  only what the project declares and the operator selects.
- WeChat/IM is not implemented in this change; the selection + execution path
  lives in the runtime (`layered_skills`, `run_turn(selected_skills=...)`,
  `runtime.mcp.activation`) so a future adapter can reuse it without a second
  agent loop.

Offline regressions: `tests/runtime/test_selected_skill_context.py`,
`tests/infra/test_skills_layered.py`, `tests/terminal/test_selection.py`,
`tests/terminal/test_palette_dynamic.py`, `tests/infra/test_mcp_config.py`,
`tests/runtime/test_mcp_activation.py`, `tests/runtime/tasks/test_headless_skills.py`.
