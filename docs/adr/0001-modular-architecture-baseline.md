# 1. Modular architecture baseline

Status: accepted (2026-10-12)

## Context

Cairn grew capability by capability: the agent loop, tools and sandbox, model
fallback, worktrees, local issue workflows, review, RepoGraph, project memory and
evaluations. Each arrived with its own owner, and several boundaries were enforced
only by convention. The next milestones — durable sessions, checkpoint and replay,
typed retry, a task queue and DAG scheduling, and MCP tool infrastructure — all
depend on those boundaries holding without another repository-wide move.

This record states the decisions a future change should not silently reverse. It
is deliberately short; commit history carries the detail of how each was applied.

## Decisions

### D1. Four areas, one direction, and a genuinely independent core

`terminal → runtime → infra → core`. `core` declares contracts, `infra` performs
external effects, `runtime` sequences work, `terminal` presents it.

`core` must not import provider SDKs, process, network or persistence modules.
An adapter's convenience is not a reason to widen a core contract. This half of
the rule was previously convention only; it is now enforced by
`tests/core/test_architecture.py`.

Consequence: a new integration adds an `infra` adapter and, only if a domain
concept is genuinely missing, a `core` contract.

### D2. One Agent turn, with a named tool-execution boundary

`runtime.loop.run_turn` remains the single owner of a turn: attempt tracing, usage
accounting, trace finalization, and conversation rollback. The per-tool sequence —
preflight, permission evaluation, authority, model-facing messages, per-tool spans
— is owned by `runtime.tool_execution`, which reports pending-call consumption
back through explicit callbacks.

Consequence: durable execution gets a real boundary to checkpoint around tool
calls. There is still no second Agent loop, and no fake persistence layer.

### D3. Ownership is expressed in the API, not by reaching into private state

Runtime verification code read `WorktreeHandle` private attributes and private
module helpers. `WorktreeHandle` now exposes what an owner legitimately needs —
`source_root`, `admin_path`, `common_dir`, `git_env`, `run_git`,
`run_git_with_env`, `probe_git_context`, `check_filters`, `check_attributes`,
`assert_registered`.

Consequence: a change to worktree internals can no longer silently break the
verification that depends on them. `run_git_with_env` is the one intentionally
weaker entry point; its docstring requires the caller to establish ownership and
context first.

### D4. Configuration separates parsing, validation, secrets and edits

`infra/config/schema.py` (shapes and validation), `infra/config/loader.py` (TOML
parsing and layout detection), `infra/config/secrets.py` (environment and credential
resolution), `infra/config/editor.py` (transactional model-file edits). `infra/config/__init__.py`
remains the single import surface.

Consequence: validating project-controlled routing stays separate from touching
credentials, and editing a local file cannot re-resolve a secret by accident.

### D5. Cooperative cancellation has one owner

`runtime.lifecycle.settle_owned` cancels unfinished owned tasks, observes every
outcome, absorbs cancellation that arrives during cleanup, and optionally
re-raises it. Call sites state their propagation policy explicitly.

`await_owned` waits without cancelling when the remaining operation is cleanup
itself. Callers keep their own error notes, result retention and cancellation
policy; sharing settlement does not merge those responsibilities. Infrastructure
cannot import runtime, so Git-owned cleanup uses `infra.git.cleanup.finish_cleanup`.

Consequence: each layer shares its cleanup mechanism without losing the
caller-specific semantics pinned by cancellation and recovery tests.

## Rejected

- **A dependency-injection container.** Construction is a few explicit functions;
  a container would add indirection without a second consumer.
- **Ports and adapters for every external effect.** Direct dependencies that are
  already narrow and stable do not need an interface.
- **A plugin framework or event bus for MCP and queue work.** Neither has an
  actual caller yet. D1–D3 create the seams; the framework can follow demand.
- **Unifying every cancellation path.** Convenience is not worth weakening
  semantics that security and recovery tests pin.
- **A compatibility layer for previous package paths.** The package layout is
  internal and has no declared external contract; the migration table was removed
  from the architecture document.

## Consequences

- New work has an obvious home, and the wrong home fails a test rather than a
  review.
- Some modules are smaller and some duplicated helpers are gone; the primary
  measure is fewer owners per lifecycle, not fewer lines.
- Known limits are recorded in
  [architecture.md](../architecture.md#known-limitations) rather than hidden.
