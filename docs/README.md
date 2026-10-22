# Cairn documentation

Start with the [README](../README.md) for what Cairn is and how to run it. This
page is the map for everything else.

## Design

| Document | Covers |
| --- | --- |
| [Architecture](architecture.md) | Package ownership, dependency direction, execution lifecycle, and where new code belongs |
| [ADR 0001: modular architecture baseline](adr/0001-modular-architecture-baseline.md) | The architectural decisions this foundation rests on, and what they deliberately reject |
| [ADR 0002: storage ownership and persistence tiers](adr/0002-storage-ownership-and-persistence.md) | Which fact each store owns, what Ephemeral, Journal and Strict Durable promise, and the storage decisions taken |
| [ADR 0003: append-only tool-fence table](adr/0003-append-only-fence-table.md) | Proposed schema V4: why a Turn's fences are one growing column, what that costs, and the append and migration contract that would replace it |

## Operating Cairn

| Document | Covers |
| --- | --- |
| [Configuration](configuration.md) | Supported `.env` and `.cairn/models.toml` layouts, provider setup, credential resolution, and local model edits |
| [M1-A model streaming](guides/model-streaming.md) | Provisional CLI text, bounded response assembly, supported provider shapes, fallback and cancellation |
| [M1-B Session lifecycle](guides/session-lifecycle.md) | Opt-in Agent ownership, turn admission, ordered events, close and settlement |
| [M1-C.1 SessionStore](guides/session-store.md) | Isolated SQLite storage, strict checkpoint codec, CAS, local lock and manual recovery facts |
| [M1-C.2 DurableSession](guides/durable-session.md) | Opt-in runtime admission, tool fences, settled checkpoint receipts and explicit host recovery |
| [M1-C.3 Durable CLI](guides/durable-cli.md) | Local creation, read-only inspection, fresh reopen and exact human reconciliation |
| [M2 Durable Tasks](guides/task-ledger.md) | Task Ledger, opt-in CodingTaskRunner execution, verified checkpoint references, explicit host-only Task reconciliation and schema upgrade |
| [GitHub Issue workflows](guides/github-workflow.md) | Retained worktrees, deterministic verification, and approved Draft PR publication |
| [GitHub Draft PR smoke preflight](guides/github-smoke-preflight.md) | Authorization list, disposable-repository preparation, and independent remote Draft PR verification |
| [Offline coding task E2E](guides/offline-coding-e2e.md) | Reproducible scripted coding task, permissions, isolation, final verification and evidence artifacts |
| [Single-node pilot runbook](guides/pilot-runbook.md) | Offline preflight, per-profile PASS/BLOCKED status, the SIGKILL inspect/confirm/reconcile drill and stop conditions |
| [Native Bash sandbox](guides/sandbox.md) | Workspace write grant, Git metadata protection, per-platform rules and unclaimed limits |
| [Safe retry](guides/safe-retry.md) | Typed tool failures and the single bounded `read_file` retry slice |
| [Coding Task summaries](guides/task-metrics.md) | Bounded JSON metrics, trace-derived call counts, verification and missing-data semantics |
| [RepoGraph](guides/repo-graph.md) | Bounded repository inventory and Python structure queries |
| [Project Memory](guides/project-memory.md) | Evidence-backed memory candidates, freshness and retrieval |
| [Session checkpoint performance](performance/session-checkpoints.md) | Checkpoint sizing options, the incremental V2 design, measurements, and verification limits |

## Experiments

[Evaluation assets](../evaluation/README.md) contains current cases, runners and
benchmarks. [Operational scripts](../scripts/README.md) covers GitHub and pilot
commands; neither directory changes the frozen experiment records below.

| Document | Covers |
| --- | --- |
| [Repository Intelligence V2](evals/repository-intelligence-v2/README.md) | The current experiment contract |
| [Repository Intelligence V1](evals/repository-intelligence-v1/README.md) | The frozen V1 contract and its 2026-10-11 pilot |

## Background

| Document | Covers |
| --- | --- |
| [SQLite runtime support](performance/sqlite-runtime-support.md) | Measured runtime versions per host, durable admission, and the release verdict |
| [Project Memory research](research/project-memory-research.md) | Historical comparison against comparable harnesses (Chinese) |

## Contributing

[AGENTS.md](../AGENTS.md) holds the working rules for coding agents, and
[tests/README.md](../tests/README.md) describes test organization. The canonical
local check set lives in [README.md](../README.md#development).

Two of those tests guard this documentation and the package boundaries:
`tests/docs/test_documentation.py` checks links and referenced paths, and
`tests/core/test_architecture.py` checks dependency direction.
