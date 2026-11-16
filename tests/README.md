# Test layout

Tests follow Cairn's package ownership:

| Package | Coverage |
| --- | --- |
| `core/` | Shared models and contracts, permissions, context budgets, and immutable request data |
| `runtime/` | Agent turns, fallback, memory lifecycle, tasks, review, and workflows |
| `infra/` | Configuration, Git and GitHub adapters, LLM clients, tools, workspace, traces, memory storage, and repository graphs |
| `terminal/` | CLI startup, provider and model selection, input handling, commands, and rendered output |
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

`tests/core/test_architecture.py` validates package dependencies offline.

Native Bash tests require a usable local shell, not a platform sandbox. They are
skipped when `/bin/bash` is absent or the host is Windows. `tests/support/sandbox.py`
provides that readiness check (the module keeps its historical name).

## Feedback cost

During development, start with the changed behavior and its real callers. Reuse
passing results while the source, test inputs and environment remain applicable.
Expand to the full suite for shared persistence, authorization, cancellation,
recovery, client boundaries or uncertain impact; do not repeat it after a
prose-only edit. A focused pass is not full acceptance.

For a full offline run on Debian, install the locked development dependencies,
build the current Web assets, then run:

```sh
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run pytest -n 2 --dist loadfile --max-worker-restart=0 \
  --cov=cairn --cov-report=term-missing --durations=30
```

Two worker processes run different files concurrently; each file stays together
in one worker. Every test is still collected and the existing branch-coverage
threshold applies to their combined coverage. Per-test temporary state remains
isolated, including state inherited by subprocesses. Shared iLink fixtures use
a distinct account ID per fake so real host-wide ownership locks remain active.
PTY input waits for raw terminal mode rather than a startup banner. Worker crashes fail the run
instead of silently retrying work. Use ordinary `uv run pytest PATH` for focused
checks or serial diagnosis. Avoid concurrent full runs on the same checkout.

Before deleting or combining tests, identify the concrete failure they catch and
which surviving test catches it through the same implementation path. Keep real
process, restart, permission and uncertain-side-effect regressions even when they
are slower. Prefer readiness signals and faster fixture cleanup to fixed sleeps;
never hide a race with automatic retries. Use `--durations=30` and JUnit reports
to distinguish setup, call and teardown costs. Test count alone is not a saving.

## CI

PRs and pushes to `main` run CI; pushing a feature branch alone does not. Open a
PR (a draft also runs CI) or use **Actions → CI → Run workflow** for pre-PR
acceptance. Each new revision cancels obsolete work on the same PR or branch.
Main still verifies the integrated commit independently of its PR.

Static checks run alongside full tests and installed-wheel acceptance, so lint
and type errors surface without waiting for the full suite. CI stops test
execution after the first failure; a successful run still executes the full
inventory. The `quality` check remains the final gate and requires every selected job to succeed. Test timings
and coverage are retained as the `test-results` artifact for seven days. Web
assets are built from this checkout before testing and installing the wheel;
this is not browser E2E acceptance.

Only changes confined to documentation Markdown under `docs/`, the root README,
`tests/README.md` and `scripts/README.md` skip executable checks. New code, removed
code, either side of a rename, prompts, fixtures, CI changes and unknown paths
require full acceptance. Empty or unavailable diffs also run full acceptance.
Manual runs always run everything. No branch protection is changed.
