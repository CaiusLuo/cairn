# Test layout

Tests are grouped by the runtime component whose contract they verify:

| Package | Coverage |
| --- | --- |
| `core/` | Agent assembly, context budgets and tool-call groups, turn execution/recovery/tracing, tool preflight, permissions and capability flow |
| `tools/` | Bash execution and sandbox enforcement, bounded file reads, guarded edits, path boundaries and tool registry |
| `cli/` | CLI configuration and interaction, input, permission choices and compact event rendering |
| `evals/` | Bounded file and content checks, coding-smoke cases, runner behavior and lifecycle |
| `llm/` | LiteLLM request/response adaptation, model-aware token counting and labelled fallback estimates |
| `observability/` | JSONL trace storage, listing/deletion, bounded prefix resolution, diagnostics and failure handling |
| `repo/` | Fresh Git repository context and bounded status output |
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

Run the repository quality checks from the root:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

For focused tests, run a package or module from the repository root:

```sh
uv run pytest tests/observability
uv run pytest tests/core/test_context.py
uv run pytest tests/evals
```

Native Bash tests require a usable platform sandbox. On macOS, the outer
environment must permit nested `sandbox-exec`; on Linux, bubblewrap must be
installed and usable. Existing readiness helpers handle unavailable sandbox
environments.
