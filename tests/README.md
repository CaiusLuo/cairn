# Test layout

Tests follow Cairn's package ownership:

| Package | Coverage |
| --- | --- |
| `core/` | Shared models and contracts, permissions, context budgets, and immutable request data |
| `runtime/` | Agent turns, fallback, evaluation, memory lifecycle, tasks, review, and workflows |
| `infra/` | Configuration, Git and GitHub adapters, LLM clients, tools, workspace, traces, memory storage, and repository graphs |
| `terminal/` | CLI startup, provider and model selection, input handling, commands, and rendered output |
| `support/` | Shared deterministic runtime doubles, Git helpers, and native sandbox readiness helpers |

Prefer tests that exercise observable behavior through the public interface.
Keep explicit coverage of authority, path boundaries, bounded resource use,
state isolation, error recovery, and cancellation cleanup. When a stronger
integration test covers the same contract, remove the duplicate helper test.
Do not pin cosmetic wording, framework defaults, or incidental call counts.
Use small parameter tables for distinct outcomes of one behavior.

Shared helpers belong in `tests.support` so one test package does not need to
import another package's test module. The architecture regression in
`core/test_architecture.py` guards the production dependency direction:
`core` depends on `core`, `infra` on `core` and `infra`, and `runtime` on those
packages plus `runtime`. The terminal may depend on the other packages.

Run repository checks from the root:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

For focused coverage, run a package or module from the repository root:

```sh
uv run pytest tests/infra/git
uv run pytest tests/runtime
uv run pytest tests/terminal/test_input.py
```

Native Bash tests require a usable platform sandbox. On macOS, the outer
environment must permit nested `sandbox-exec`; on Linux, bubblewrap must be
installed and usable. Existing readiness helpers handle unavailable sandbox
environments.
