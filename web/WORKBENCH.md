# Cairn Web Workbench

The product reference is Tencent LoopForge at `ffaed1574eb4a232581d0ee1cbba46cc01fb2675`. Cairn starts from `73021ba56c4949ffa88cb4e81d3fa690a3a20629`. The reference is pinned for review. [LoopForge source](https://github.com/Tencent/LoopForge/tree/ffaed1574eb4a232581d0ee1cbba46cc01fb2675)

| LoopForge surface | Cairn mapping | Reuse |
| --- | --- | --- |
| `App.jsx`, `SidebarPreview.jsx`, `styles.css`, `design.md` | Navigation rail, collapsible conversation tree, centered composer, settings shell | Layout dimensions, responsive rules and interaction reference |
| `HarnessIcon.jsx`, `hui-tokens.css`, `harness-ui.css` | SVG icon renderer, theme and component tokens | Selected licensed icon geometry and chat token stylesheet |
| `useChat.js`, `ActivityTrace.jsx` | Shared Conversation, Run event cursors, streaming preview and tool timeline | Interaction pattern with Cairn transport and identities |
| `ArtifactPanel.jsx`, `SopRunPanel.jsx` | Drawer for committed results and execution evidence, Run and Task detail | Drawer and disclosure patterns |
| `flows.js`, `flow-viewport.js`, `flow-history.js`, `flow-palette.js`, `flow-canvas.css` | Read-only execution graph with a local arrangement and inspector | Coordinate and history approach, graph presentation |
| `internal/domain/workflow.go`, `run.go` | Future versioned workflow definition and snapshot, WorkflowRun and NodeRun attempts | Adapt model boundaries in Python after review |
| `workflow_graph.go`, `node_execute.go` | Future DAG validation and readiness over the existing single TaskService worker | Adopt topological validation and upstream readiness, reuse LangGraph |
| `node_outcome.go`, `node_outputs.go`, `gate_submit.go` | Future bounded attempts, artifact contracts and separate human decisions | Adapt semantics to checkpoint receipts and explicit Server authorization |
| Runner leases across workers, Docker executors, subagent orchestration | Cairn already has host ownership, Worktree isolation and a shared Agent harness | Outside the current scope |

The first two phases deliver the Shell and integrate existing Server contracts. Model execution and durable history stay in Cairn. The current Server configuration binds a single workspace. A read-only workspace chooser shows the host binding.

See [asset provenance](src/vendor/harness/SOURCE.md) and the MIT notices under `public/licenses/`.
