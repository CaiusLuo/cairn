# Test layout

Tests follow Cairn's package ownership:

| Package | Coverage |
| --- | --- |
| `core/` | Shared models and contracts, permissions, context budgets, and immutable request data |
| `runtime/` | Agent turns, fallback, evaluation, memory lifecycle, tasks, review, and workflows |
| `infra/` | Configuration, Git and GitHub adapters, LLM clients, tools, workspace, traces, memory storage, and repository graphs |
| `terminal/` | CLI startup, provider and model selection, input handling, commands, and rendered output |
| `docs/` | Documentation navigation: relative links, referenced source paths, and documented test commands |
| `support/` | Shared deterministic runtime doubles, Git helpers, local-shell readiness helpers, and WeChat service fixtures |
| `runtime/test_wechat_service*.py` | WeChat service execution, chat routing, recovery and detached-owner regressions |

Prefer tests that exercise observable behavior through the public interface.
Keep explicit coverage of authority, path boundaries, bounded resource use,
state isolation, error recovery, and cancellation cleanup. When a stronger
integration test covers the same contract, remove the duplicate helper test.
Do not pin cosmetic wording, framework defaults, or incidental call counts.
Use small parameter tables for distinct outcomes of one behavior.

Shared helpers live in `tests.support`. Current import-boundary checks are in
`core/test_architecture.py`; update them alongside intentional package changes.

Run repository checks from the root; the canonical command list is in
[README.md](../README.md#development).

For focused coverage, run a package or module from the repository root:

```sh
uv run pytest tests/infra/git
uv run pytest tests/runtime
uv run pytest tests/terminal/test_input.py
```

`tests/docs` validates the documentation itself, and
`tests/core/test_architecture.py` validates package boundaries and `core`
independence. Both are fast and offline.

Native Bash tests require a usable local shell, not a platform sandbox. They are
skipped when `/bin/bash` is absent or the host is Windows. `tests/support/sandbox.py`
provides that readiness check (the module keeps its historical name).
