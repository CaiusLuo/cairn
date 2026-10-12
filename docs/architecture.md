# Architecture

Cairn uses four package areas to keep contracts, execution, external systems and
the command line easy to locate. They describe dependency direction, not a
requirement that every use case pass through an abstract interface.

Arrows point from a package to its allowed dependencies:

```mermaid
flowchart LR
    terminal --> runtime
    terminal --> infra
    terminal --> core
    runtime --> infra
    runtime --> core
    infra --> core
```

`core` owns domain models, policies and interfaces. It may import only other
`core` modules, and must not import provider SDKs or process, network and
persistence modules. `infra` implements external capabilities and may import
`core` and `infra`. `runtime` composes those capabilities and owns execution; it
may import `core`, `infra` and `runtime`. `terminal` is the command-line adapter
and may compose all three.

`tests/core/test_architecture.py` enforces both the direction and the `core`
import rule. There are no compatibility modules preserving obsolete package
paths; see [ADR 0001](adr/0001-modular-architecture-baseline.md) for why.

## Execution ownership

The CLI enters through `cairn.terminal.cli`; `cairn.runtime.assembly` builds the
session's Agent and its dependencies. `cairn.runtime.loop.run_turn` is the single
owner of the Agent turn: it records spans, handles failures and usage, and
finalizes the turn. Tool calls inside an accepted response are executed by
`cairn.runtime.tool_execution`, which owns preflight, permission evaluation,
execution authority, model-facing tool messages and per-tool spans. `run_turn`
keeps sole ownership of conversation rollback and of which calls are still
pending when a turn aborts.

`ModelExecutor` selects the matching model, token counter and provider runtime for
each candidate, builds that candidate's request, and returns `(request, response)`.
It does not own turn tracing, turn error finalization or turn lifecycle.

Task, workflow, review and evaluation code in `cairn.runtime` provide bounded
orchestration around Agent turns. They do not create a second general Agent loop.
Existing scheduling and retry behavior belongs with the runtime flow that uses it.
Infrastructure supplies capabilities such as model clients, filesystem/worktree
operations, GitHub transport and trace storage; it does not decide when a task or
turn should run.

## Cancellation

Work the harness owns is allowed to finish its cleanup when the caller cancels,
and repeated cancellation never interrupts that cleanup.
`cairn.runtime.lifecycle.settle_owned` owns that sequence; each call site states
whether a late cancellation propagates. `cairn.infra.git.worktree` uses the same
invariant for worktree release through its own `_finish`, because `infra` may not
import `runtime`.

## Package guide

| Area | Put here |
| --- | --- |
| `cairn.core` | Agent-facing contracts and stable domain data, including LLM, task, memory and repository models |
| `cairn.runtime` | Agent/turn execution and the tool-call boundary, assembly, model selection at execution time, and bounded task, workflow, review, evaluation and memory orchestration |
| `cairn.infra` | Configuration, provider implementations, token counting, tools, Workspace and Git operations, GitHub transport, RepoGraph and observability storage |
| `cairn.terminal` | CLI startup, terminal input/output and user commands |
| `cairn.resources` | Packaged static resources such as the terminal banner |

When adding a feature, put its domain types in `core` only when they are
meaningful independently of a particular adapter. Put operating-system,
provider, network, persistence or process details in `infra`. Put sequencing,
admission, retries and coordination in the runtime that owns the operation. Add
CLI parsing and presentation in `terminal`. Keep the narrowest useful boundary;
do not add ports or adapters where a direct dependency is already clear and
stable.

## Where mutable state lives

| State | Owner |
| --- | --- |
| Conversation messages | `core.state.AgentState`, held by one `runtime.agent.Agent` |
| Session model selection and grants | `core.model_manager` and `core.permissions.SessionPermissionHandler` |
| Active provider runtime | `terminal.cli`, committed only through its `activate` step |
| Per-model failure notes for `/model list` | `runtime.model_executor.ModelExecutor` |
| Turn spans and usage | `runtime.loop.run_turn` |
| Task status and results | `runtime.tasks.CodingTaskRunner` |
| Workflow and review progress reports | `_Progress` in each workflow, persisted atomically |
| Worktree lifecycle | `infra.git.worktree.WorktreeHandle` |

## Extension seams for the next milestones

- Durable sessions and checkpoint/resume: a turn boundary (`run_turn`) and a
  tool-call boundary (`tool_execution`) already exist to checkpoint around;
  `AgentState` is the conversation owner to serialize.
- Typed retry, timeout and idempotency: model fallback lives in `ModelExecutor`,
  task-level cancellation in `lifecycle`, and workflow timeouts at their call
  sites.
- Task queue and DAG scheduling: `CodingTaskRunner` and the workflow runners own
  their lifecycles; a scheduler should call them rather than duplicate them.
- MCP and external tools: `infra.tools` and `infra.tools.registry` own tool
  contracts and registration; permission decisions stay in `core.permissions`.

## Known limitations

- `core.models.ToolResult` is a subprocess-shaped contract (`stdout`, `stderr`,
  `exit_code`). Non-shell tools must fit it, and a richer structured result has no
  core representation yet. Changing this touches every tool and is deferred until
  a tool actually needs it.
- `core.permissions` knows the `bash` tool name and its argument keys. This is a
  deliberate, narrow guardrail that runs after the tool validates its arguments,
  so the coupling is documented rather than removed.
- `core.events.Event.data` is an untyped mapping rendered and consumed by string
  key. A typed payload per event would be a real improvement, but no consumer
  needs it yet.

## References

The package boundaries are Cairn's own pragmatic organization. Two useful
external examples are [mini-swe-agent's source tree](https://github.com/SWE-agent/mini-swe-agent/tree/main/src/minisweagent),
which separates agents, models, environments, run logic and utilities, and
[`pydantic-ai`'s package](https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim/pydantic_ai),
which separates agents, models, providers, toolsets, workspaces and its CLI.
The useful lesson is to make execution, provider/environment details and entry
points findable; these projects do not prescribe Cairn's exact layer layout.
