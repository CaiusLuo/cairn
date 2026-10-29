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
| [Session discovery, titles and resumed work](guides/session-discovery.md) | Listing stored Sessions, abbreviated IDs, title contract and the Task/goal resumption order |
| [M2 Durable Tasks](guides/task-ledger.md) | Task Ledger, opt-in CodingTaskRunner execution, verified checkpoint references, explicit host-only Task reconciliation and schema upgrade |
| [GitHub Issue workflows](guides/github-workflow.md) | Retained worktrees, deterministic verification, and approved Draft PR publication |
| [GitHub Draft PR smoke preflight](guides/github-smoke-preflight.md) | Authorization list, disposable-repository preparation, and independent remote Draft PR verification |
| [Offline coding task E2E](guides/offline-coding-e2e.md) | Reproducible scripted coding task, permissions, isolation, final verification and evidence artifacts |
| [Single-node pilot runbook](guides/pilot-runbook.md) | Offline preflight, per-profile PASS/BLOCKED status, the SIGKILL inspect/confirm/reconcile drill and stop conditions |
| [Interactive terminal](guides/tui-interaction.md) | Transcript grammar, composer keys, steering a running turn, and what startup asks |
| [Server single-task entry](guides/server-task-entry.md) | Non-interactive `cairn task run` for Linux servers: explicit workspace, authorization flags, machine-readable result and exit codes |
| [Local command execution](guides/local-execution.md) | Host shell, approval, process guarantees and the boundaries it does not claim |
| [Safe retry](guides/safe-retry.md) | Typed tool failures and the single bounded `read_file` retry slice |
| [Coding Task summaries](guides/task-metrics.md) | Bounded JSON metrics, trace-derived call counts, verification and missing-data semantics |
| [Tracing and opt-in OTLP export](guides/tracing-and-otlp.md) | Session/task/turn correlation, compaction/fallback trigger reasons, default-off OTLP export and redaction |
| [RepoGraph](guides/repo-graph.md) | Bounded repository inventory and Python structure queries |
| [Project Memory](guides/project-memory.md) | Evidence-backed memory candidates, freshness and retrieval |
| [Session checkpoint performance](performance/session-checkpoints.md) | Checkpoint sizing options, the incremental V2 design, measurements, and verification limits |
| [Schema V4 fence performance](performance/schema-v4-fences.md) | V3/V4 append cost, the per-stage breakdown, and what the numbers do not claim |

## Experiments

[Evaluation assets](../evaluation/README.md) contains current cases, runners and
benchmarks. [Operational scripts](../scripts/README.md) covers GitHub and pilot
commands; neither directory changes the frozen experiment records below.

| Document | Covers |
| --- | --- |
| [Repository Intelligence V2](evals/repository-intelligence-v2/README.md) | The current experiment contract |
| [Coding behavior V1](evals/coding-behavior-v1/README.md) | The versioned `graded-behavior-v2` behavioral acceptance for the five coding-smoke prompts |
| [Real-model compaction comparison](evals/compaction-live-comparison/README.md) | The opt-in full-context vs real-summary continuation comparison (LIVE_NOT_RUN this round) |
| [Terminal-Bench 2.0 / Harbor investigation](evals/terminal-bench-harbor-investigation/README.md) | Contract mapping, thin-vs-architecture split and the owner decision point |
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
