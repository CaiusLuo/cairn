# Test layout

Tests are grouped by the runtime component whose contract they verify:

| Package | Coverage |
| --- | --- |
| `core/` | Agent assembly, turn execution/recovery/tracing, tool preflight, permissions and capability flow |
| `tools/` | Bash execution and sandbox enforcement, file operations, tool registry |
| `cli/` | CLI configuration and interaction, input, permission choices and compact event rendering |
| `evals/` | Final-state checks, runner behavior and lifecycle |
| `llm/` | LiteLLM request/response adaptation |
| `observability/` | Trace storage round-trip, listing and safe lookup |
| `repo/` | Git repository context |
| `workspace/` | Workspace roots and directory ownership |
| `support/` | Shared deterministic runtime doubles and native sandbox readiness helpers |

Name test modules after the behavior or component within their package. Keep
test functions descriptive. Cross-component tests belong to the package whose
contract they exercise; for example, NETWORK permission flow stays in `core/`,
while Bash sandbox enforcement stays in `tools/`.

Prefer tests that exercise observable behavior through the public interface.
Keep explicit coverage of authority, path boundaries, bounded resource use,
state isolation, error recovery and cancellation cleanup. When a stronger
integration test covers the same contract, remove the duplicate helper test.
Do not pin cosmetic wording, framework defaults or incidental call counts.
Use small parameter tables for distinct outcomes of one behavior.

Shared helpers use explicit imports from `tests.support.runtime` or
`tests.support.sandbox`. Package markers keep imports unambiguous for pytest
and mypy.

Run the full suite or select a package/module from the repository root:

```sh
uv run pytest
uv run pytest tests/evals
uv run pytest tests/core/test_tool_preflight.py
uv run mypy src tests
uv run ruff check src tests
uv run ruff format --check src tests
```

Native Bash tests require a usable platform sandbox. On macOS, the outer
environment must permit nested `sandbox-exec`; on Linux, bubblewrap must be
installed and usable. Existing readiness helpers handle unavailable sandbox
environments.
