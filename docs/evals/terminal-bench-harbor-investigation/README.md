# Terminal-Bench 2.0 / Harbor: minimal-adapter investigation

**Decision: DEFER to the owner (decision point).** A thin Cairn adapter that reuses
existing contracts is possible, but a *useful* integration needs container
orchestration and a task/agent-rollout interface that Cairn does not own today.
That is an architecture change, not a thin adapter, so this is handed back rather
than forced.

Sources: [Terminal-Bench 2.0 and Harbor announcement](https://www.tbench.ai/news/announcement-2-0),
Harbor docs (harborframework.com).

## What Terminal-Bench 2.0 + Harbor are

- **Terminal-Bench 2.0** is a verified, harder revision of the Terminal-Bench task
  set: each task is a containerized environment plus tests, with a
  `task.yaml`-style definition and a `solution.yaml` under
  `terminal-bench/` (the benchmark GUID warning is part of the dataset's own
  rule, not Cairn's).
- **Harbor** is the harness: it runs containers (locally or cloud-scaled), exposes
  a rollouts interface for RL/SFT, and accepts "any agent that can be installed in
  a container".

## Where Cairn already matches

| Harbor/TB-2.0 need | Cairn today |
| --- | --- |
| A container per task | No container orchestration; `BashTool` is local, no OS sandbox |
| An "agent" callable inside the container | `runtime.loop.run_turn` is a process-local async coroutine, not a container entrypoint |
| Task environment/tests as data | Manifest + digest pattern exists (SWE-bench subset, RI dataset) |
| Prediction/rollout artifact | Thin JSONL/diff adapters exist (SWE-bench, compaction) |
| Budget/usage record | `RunBudget`, `ContextBudget`, `turn_trace` usage exist |

## Thin adapter: feasible (smaller piece)

A **prediction/trajectory adapter** is genuinely thin and reuses Cairn's existing
contracts:

- reuse the manifest+digest pattern from
  [SWE-bench Lite subset](../swebench-lite-subset/README.md) to pin a fixed
  Terminal-Bench task subset;
- reuse the prediction-JSONL pattern for a per-task result artifact;
- reuse `run_budget`/`turn_trace` usage to record budget and timing.

That piece is like the SWE-bench adapter and could be done without new
architecture.

## Why the full integration is not a thin adapter

Running Cairn as a Harbor agent requires an **entrypoint inside the task
container** with the task's environment, deterministic teardown and a rollout
record — that is a runtime/execution boundary Cairn does not currently own.
Building it means either:

1. a container-execution layer in `infra` (new effect owner, new safety checks),
   and
2. a Harbor-facing rollout adapter that spawns Cairn in that container and
   serializes its trajectory.

Both are material architecture additions, and neither is justified by a caller
that exists today (AGENTS.md: do not add ports/registries without a caller).

## Decision point for the owner

Choose one:

- **(A) Thin artifact adapter only** — pin a TB-2.0 subset and emit results in a
  Harbor-compatible artifact shape; no container execution by Cairn. Low risk,
  small, reuses existing contracts.
- **(B) Full Harbor agent integration** — add a container-execution owner in
  `infra` and a Harbor rollout adapter. Requires an explicit architecture
  decision (effect ownership, isolation guarantees, teardown/cancellation),
  container/VM infrastructure, and a real budget.
- **(C) Defer** — keep this investigation as the record and revisit when a
  container-execution requirement appears for another reason.

Recommendation: (A) if an artifact is needed now; otherwise (C). (B) should start
only with an explicit owner authorization and an architecture note, not as a
"minimal adapter".

## Status

No Terminal-Bench/Harbor code was written this round. This document is the
deliverable: the contract mapping, the feasibility split, and the decision point.
