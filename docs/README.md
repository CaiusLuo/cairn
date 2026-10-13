# Cairn documentation

Start with the [README](../README.md) for what Cairn is and how to run it. This
page is the map for everything else.

## Design

| Document | Covers |
| --- | --- |
| [Architecture](architecture.md) | Package ownership, dependency direction, execution lifecycle, and where new code belongs |
| [Decisions](adr/0001-modular-architecture-baseline.md) | The architectural decisions this foundation rests on, and what they deliberately reject |
| [Durable Session V1 design](design/durable-session-v1.md) | Proposed committed-turn persistence, safe recovery and implementation slices; pending architecture review |

## Operating Cairn

| Document | Covers |
| --- | --- |
| [Configuration](configuration.md) | Supported `.env` and `.cairn/models.toml` layouts, provider setup, credential resolution, and local model edits |
| [M1-A model streaming](guides/model-streaming.md) | Provisional CLI text, bounded response assembly, supported provider shapes, fallback and cancellation |
| [M1-B Session lifecycle](guides/session-lifecycle.md) | Opt-in Agent ownership, turn admission, ordered events, close and settlement |
| [M1-C.1 SessionStore](guides/session-store.md) | Isolated SQLite storage, strict checkpoint codec, CAS, local lock and manual recovery facts |
| [M1-C.2 DurableSession](guides/durable-session.md) | Opt-in runtime admission, tool fences, settled checkpoint receipts and explicit host recovery |
| [M1-C.3 Durable CLI](guides/durable-cli.md) | Local creation, read-only inspection, fresh reopen and exact human reconciliation |
| [M2-A Task Ledger](guides/task-ledger.md) | Durable Task phase facts, verified checkpoint references, read-only recovery reports and explicit schema upgrade |
| [GitHub Issue workflows](guides/github-workflow.md) | Retained worktrees, deterministic verification, and approved Draft PR publication |
| [RepoGraph](guides/repo-graph.md) | Bounded repository inventory and Python structure queries |
| [Project Memory](guides/project-memory.md) | Evidence-backed memory candidates, freshness and retrieval |

## Experiments

| Document | Covers |
| --- | --- |
| [Repository Intelligence V2](evals/repository-intelligence-v2/README.md) | The current experiment contract |
| [Repository Intelligence V1](evals/repository-intelligence-v1/README.md) | The frozen V1 contract and its 2026-10-11 pilot |

## Background

| Document | Covers |
| --- | --- |
| [Project Memory research](research/project-memory-research.md) | Historical comparison against comparable harnesses |

## Contributing

[AGENTS.md](../AGENTS.md) holds the working rules for coding agents, and
[tests/README.md](../tests/README.md) describes test organization. Local checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

Two of those tests guard this documentation and the package boundaries:
`tests/docs/test_documentation.py` checks links and referenced paths, and
`tests/core/test_architecture.py` checks dependency direction.
