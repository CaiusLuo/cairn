# Working rules for coding agents

Cairn is a Python 3.12 agent harness. Use [docs/README.md](docs/README.md) to find
current contracts. Read [architecture.md](docs/architecture.md) and
[ADR 0001](docs/adr/0001-modular-architecture-baseline.md) before changing package
boundaries or execution ownership; subsystem details belong in linked guides.

## Start with evidence

Before editing, establish the branch, HEAD and existing work:

```bash
git status --short --branch
git rev-parse HEAD
git log --oneline --decorate -5
git diff --stat
git diff --check
```

- Compare against the task's actual base ref using targeted diffs and symbols.
  Check whether the behaviour already exists, its owner and tests before full reads.
- Preserve pre-existing edits and staged/unstaged boundaries. Do not reset, clean,
  stash, rebase or overwrite existing work without explicit authorization.
- If the checkout contradicts the requested branch or baseline, report the
  discrepancy before modifying files. Do not silently switch or reconstruct it.

## Environment

Reference commands; choose the verification scope under **Verification** below.

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Run `pytest` with explicit regression and related test paths by default. The full
offline suite command, when justified, is
`uv run pytest --cov=cairn --cov-report=term-missing`.

`pytest` is offline by default. Paid provider runs and external publication need
separate, explicit authorization. Use the authorized execution environment; do
not introduce model proxies or paid subprocesses.

## Architecture rules

- Dependencies point one way: `terminal → runtime → infra → core`. Within Cairn,
  `core` may import only `core`; `tests/core/test_architecture.py` enforces this.
- `core` holds contracts that are meaningful without an adapter. It must not
  import provider SDKs, process, network or persistence modules. Do not add a
  field to a core model because one adapter finds it convenient.
- `infra` performs external effects and owns their safety checks. `runtime`
  sequences work and owns task/turn state. `terminal` presents and parses input.
- Put sequencing in the runtime that owns the operation. Do not add ports,
  registries, factories or event buses without a caller that needs them today.
- Every mutable state, lifecycle and external side effect needs one owner. If two
  modules both advance a lifecycle, that is a defect, not a convenience.

## Behaviour rules

- `runtime.loop.run_turn` is the only Agent turn. Do not add a second loop.
- It owns pending calls, conversation rollback and turn completion. Tool execution
  and turn telemetry serve that boundary; they must not independently finalize a
  turn or discard executed tool facts after a later failure.
- Tool preflight (existence and argument validity) must precede permission
  checks; authority comes only from granted capabilities, never from model
  arguments.
- Fail closed at trust boundaries: sandbox, path validation, credential
  filtering, publication guards and evidence checks. Do not remove a check
  because it looks duplicated; first establish whether it guards the same trust
  boundary.
- Runtime-owned work must finish cleanup under repeated cancellation. Use
  `cairn.runtime.lifecycle`; cancel abandoned work with `settle_owned`, and finish
  cleanup itself with `await_owned`. Preserve caller-specific primary errors,
  late cancellation and retention policies. Git infrastructure uses its own
  `cairn.infra.git.cleanup` boundary and must not import runtime.
- TOML parsing must not resolve credentials; invalid project configuration must
  not fall back silently. Persistent edits must preserve comments and ordering,
  and must not mutate the approved session. See [configuration](docs/configuration.md).
- Build a provider/model replacement before committing active bindings. Rebuild
  admission for each fallback candidate with its matching client, token counter,
  complete request overhead and output reserve; preserve full conversation state.
- Structural changes must not change Agent semantics or evaluation outcomes.

## Implementation workflow

- State the concrete defect, owning component, intended behaviour and affected
  invariants before a substantial change. Inspect existing callers before adding
  an abstraction; file size alone does not justify splitting a responsibility.
- Work in coherent slices. Add regression coverage for the real failure, run
  focused checks, then review the diff for semantic changes before starting an
  unrelated slice. Compare critical refactor paths to the established baseline.
- Keep edits within the task; do not combine unrelated cleanup. Update current
  contract documentation when an API or guarantee changes.
- Stop when acceptance criteria are met; defer speculative integrations.

## Verification

Organize tests by ownership, mirroring `src/cairn`. Shared fixtures live in
`tests/support/`; do not import one test module from another. Keep security,
cancellation, recovery, sandbox and freshness coverage intact even when pruning
redundant tests. See [tests/README.md](tests/README.md).

- Exercise observable contracts through public interfaces. Do not replace a
  failure test with a happy path or pin incidental wording and call counts.
- Run new or changed regression tests and related tests for the affected owners,
  callers and invariants. This scoped set is the default completion gate; do not
  automatically run the full suite for each change or at integration.
- Expand verification only when the user requests it or a concrete failure or
  cross-module risk requires broader coverage. Explain that reason before running
  broader checks. Repeat passing checks only for a new change, failure or
  unresolved concern. Explain removed tests and coverage loss.
- Documentation-only changes normally need link/path tests and a diff review;
  optional runtime and packaging checks depend on the affected contract.

| Area | Required evidence |
| --- | --- |
| CLI or packaging | Public CLI smoke; for entrypoint/import/package changes, `uv build`, a clean wheel install, `cairn --help` and representative imports |
| Git or worktrees | Temporary local repositories; source files/index/HEAD preserved, registration and branch ownership, bounded pipes, timeouts, spawn cancellation and repeated cleanup cancellation |
| Tools, permissions or sandbox | Preflight before approval, denial/no-handler paths, capability-derived authority, credential filtering and writable-root/path escape cases |
| Persistence or Memory | Schema/transaction/CAS and recovery paths as applicable; trusted-host approval, checkout/tree provenance, freshness and storage outside every sandbox-writable root |
| Model fallback or context | Ordered candidates, matching runtimes, complete request budgets, admission failures, intact tool message groups and unknown provider usage |
| Evals | Deterministic workspace checks, case/arm isolation, cancellation artifacts, missing-data semantics and frozen dataset digests |
| Publication | Explicit authorization plus current passing verification bound to the exact tree/diff; blocked or interrupted publication retains local evidence |

Trust boundaries are detailed in [GitHub workflows](docs/guides/github-workflow.md),
[Project Memory](docs/guides/project-memory.md) and [RepoGraph](docs/guides/repo-graph.md).
LLM verdicts cannot replace deterministic checks or evidence validation; assistant
text cannot approve Memory, grant network access or authorize publication.
Retained graph snapshots are historical after an incomplete refresh; retrieval
must check the new candidate.

## Git and review handoff

- Commit only when the task authorizes it; use focused Conventional Commit
  messages and stage explicit paths. Do not include unrelated user edits.
- Push only to the authorized repository/branch after checks and branch ancestry
  verification. Never silently force-push, merge, close issues or publish resources.
  Review readiness is not merge authorization.
- Report the starting/ending HEAD, affected contracts, exact checks and results,
  known limitations and the next independent review action. Distinguish local
  commits, pushed commits, remote CI and unverified live operations.

## Documentation and experiments

- Documentation describes the current design, not the session that produced it.
  Change history belongs in commits.
- Update links when you move a file; `tests/docs/test_documentation.py` checks
  relative links and referenced paths.
- Never modify frozen evidence: the Repository Intelligence V1 pilot directory,
  its experiment records, dataset digest or historical findings.
- Changed fixtures, prompts or acceptance oracles require an explicitly versioned
  dataset/check contract. Preserve the current [V2 contract](docs/evals/repository-intelligence-v2/README.md);
  do not reinterpret frozen V1 runs with a new oracle.
- Report final workspace correctness separately from within-budget termination.
  Keep unavailable observations null; interrupted/unbalanced pilots and offline
  doubles do not establish real-model improvement.

## Definition of done

For code changes, implement the requested behaviour and regression coverage;
pass regression and related tests plus applicable Ruff/format/Mypy checks;
run applicable extra checks above; update changed contracts; and review the diff
for unintended authority, sandbox, evidence or lifecycle changes. Disclose
limitations and blocked checks.

Return the commit SHA or tested working-tree state; report only observed results.
Delegate only with task authorization and bounded assignments; the integrating
agent still owns diff review, validation and this completion gate.

## Module placement and public entry points

- Keep the native Python CLI in `terminal`; retired Pi/Bridge and ACP adapters
  must not be restored as compatibility layers.
- Import configuration through `cairn.infra.config`; its `schema`, `loader`,
  `secrets` and `editor` modules own validation, parsing, credential resolution
  and transactional edits respectively.
- `cairn.infra.tools.build_default_registry` owns the ordered built-in tool
  collection. `runtime.assembly.build_agent` binds it to the Agent and repository
  context. Registry construction grants no permissions; `run_turn` and
  `tool_execution` retain admission, preflight and execution authority.
- Core modules follow domain ownership, not directory symmetry: conversation
  state, request context, model selection, Task values, LLM/events and memory
  contracts stay independent of adapters. Do not combine them into shared state.
- Local Git and remote GitHub must not import one another. Session and Memory
  stores must not import one another; neutral SQLite qualification belongs in
  `infra.sqlite_policy`. Trace storage remains telemetry, per ADR 0002.
- Reusable execution belongs in `runtime.evals`; repository experiment cases,
  runners and benchmarks belong in `evaluation/`, and operational GitHub/pilot
  commands in `scripts/`. Frozen `docs/evals/` evidence remains historical.
