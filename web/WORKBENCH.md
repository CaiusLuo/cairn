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

## Phase B data map

| Workbench area | Server surface | Data owner |
| --- | --- | --- |
| Workspace and model options | `GET /api/runtime/management?section=workbench` | Host workspace, approved provider group and execution policy. The picker changes the requested model for a Conversation Run and keeps the existing provider fallback policy. |
| Conversation and history | `GET/POST /api/runtime/conversations`, `GET /conversations/{id}/history`, `POST /conversations/{id}/messages` | ConversationService and DomainStore. CLI, Web and WeChat share IDs and stored history. |
| Runs and execution trace | `GET /api/runtime/runs`, `GET /runs/{id}`, `GET /runs/{id}/events`, Run WebSocket | DomainStore and RunLedger. The list is bounded to the latest 100 Runs in the Server workspace. The live channel pairs preview text with ordered execution events. |
| Development Task and Worktree | `GET/POST /api/runtime/tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/events`, `POST /tasks/{id}/messages` | TaskService and RuntimeCatalogue, with persisted checkpoints and existing domain projection. |
| Task execution graph | Same Task event stream | The graph uses recorded event sequences. Layout history is page-local. Edges show chronological order and cannot be edited or executed. |
| Recovery review | Task/Run `GET recovery` and `POST reconcile` | Existing checkpoint review and Task sequence or Run revision. Writes require a valid operator session on the same origin or the runtime bearer. |
| Evidence drawer | Persisted Run/Task result and available tool outputs from history | Existing runtime state and Session checkpoint history. The Run drawer scopes evidence to its recorded result. |
| Settings | `/api/runtime/management` and `/api/runtime/commands` | Existing server controls for WeChat, schedules and memory. |

Paths below `/api/runtime/` in the table use that same prefix. Submitted browser requests keep their original IDs for explicit retry after a dropped response. Unsent queue items stay in the tab. The existing page addresses share `src/main-workbench.tsx`. The shell, chat, task and run views, recovery review and graph live under `src/workbench/`. The former Kanban, Session browser, independent Chat/Task panels, redundant snapshot client and their styles were removed. The legacy Server snapshot and task-board API were left available to their existing callers.

## Review boundary

The next phase needs a versioned `WorkflowDefinition` containing `WorkflowNode` and `WorkflowEdge` records, an immutable `WorkflowSnapshot` per `WorkflowRun`, and a link from WorkflowRun to the existing Development Task. Conversation and Run retain their current meanings. A `NodeRun` records one node execution, with bounded `Attempt` records and a stable key formed from workflow run, node and attempt. Forward dependency edges support topological validation. Rework uses separate transitions with explicit limits. Each `ArtifactContract` declares typed output, downstream inputs and bounded content reads. Each `ApprovalDecision` should retain the authenticated operator, expected revision, decision and receipt. The graph should become editable only after the definition and revision contracts are available.

The current Development Handoff can accept a Task in RuntimeCatalogue before all related domain facts are committed. Resolve that window with an idempotent durable admission and recoverable projection, then test crashes across each write and reconcile unknown effects without replay. Automatic DAG execution stays gated on that work. The future scheduler can reuse TaskService, one worker and LangGraph. LoopForge's leases for multiple workers and separate runner infrastructure add no current product value.

Workflow approvals, structured test results, task dependency edges and bounded worktree file/Diff reads need Server contracts. Phase B displays the current Task/Run recovery review, recorded tool output and chronological execution graph.

## Browser verification

Build Web first. Run `env -u TMPDIR uv run python -m tests.support.workbench_browser --channel chrome` against the isolated fixture on macOS, or omit `--channel` after installing Playwright Chromium. The fixture starts a Server and real CLI process, forces one crash after a tool effect and verifies the recovery note and untouched artifact after another restart. Optional `--screenshots /absolute/path` saves desktop and narrow captures. The browser fixture creates WeChat-source Runs through the host API. The actual Server receiver with a Fake iLink endpoint is covered by `tests/terminal/test_wechat_e2e_process.py` on Linux.

To exercise the real model path against an isolated provider-configured Server, set `CAIRN_WEB_E2E_URL`, `CAIRN_WEB_E2E_TOKEN_FILE` and `CAIRN_WEB_E2E_REAL_MODEL=1`, then run `pnpm --dir web test:e2e`. This submits one model request.
