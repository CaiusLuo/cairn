# Cairn Web Workbench

The product reference is Tencent LoopForge at `ffaed1574eb4a232581d0ee1cbba46cc01fb2675`. Cairn starts from `73021ba56c4949ffa88cb4e81d3fa690a3a20629`. The reference is pinned for review. [LoopForge source](https://github.com/Tencent/LoopForge/tree/ffaed1574eb4a232581d0ee1cbba46cc01fb2675)

| LoopForge surface | Cairn mapping | Reuse |
| --- | --- | --- |
| `App.jsx`, `SidebarPreview.jsx`, `styles.css`, `design.md` | Navigation rail, collapsible conversation tree, centered composer, settings shell | Layout dimensions, responsive rules and interaction reference |
| `HarnessIcon.jsx`, `hui-tokens.css`, `harness-ui.css` | SVG icon renderer, theme and component tokens | Selected licensed icon geometry and chat token stylesheet |
| `useChat.js`, `ActivityTrace.jsx` | Shared Conversation, Run event cursors, streaming preview and tool timeline | Interaction pattern with Cairn transport and identities |
| `ArtifactPanel.jsx`, `SopRunPanel.jsx` | Drawer for committed results and execution evidence, Run and Task detail, Knowledge clip detail | Drawer and disclosure patterns |

The Shell integrates existing Server contracts. Model execution and durable history stay in Cairn. The current Server configuration binds a single workspace. A read-only workspace chooser shows the host binding.

See [asset provenance](src/vendor/harness/SOURCE.md) and the MIT notices under `public/licenses/`.

## Data map

| Workbench area | Server surface | Data owner |
| --- | --- | --- |
| Workspace and model options | `GET /api/runtime/management?section=workbench` | Host workspace, approved provider group and execution policy. The picker changes the requested model for a Conversation Run and keeps the existing provider fallback policy. |
| Conversation and history | `GET/POST /api/runtime/conversations`, `GET /conversations/{id}/history`, `POST /conversations/{id}/messages` | ConversationService and DomainStore. CLI, Web and WeChat share IDs and stored history. Rename, archive and delete are available from the conversation tree. |
| Runs and execution trace | `GET /api/runtime/runs`, `GET /runs/{id}`, `GET /runs/{id}/events`, Run WebSocket | DomainStore and RunLedger. The list is bounded to the latest 100 Runs in the Server workspace. The live channel pairs preview text with ordered execution events. |
| Development Task and Worktree | `GET/POST /api/runtime/tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/events`, `POST /tasks/{id}/messages` | TaskService and RuntimeCatalogue, with persisted checkpoints and existing domain projection. Task detail keeps the execution timeline and recovery review. |
| Knowledge clips | `GET/POST /api/knowledge/clips`, `GET/POST /api/knowledge/clips/{id}`, `POST /api/knowledge/clips/{id}/update|archive|unarchive`, `GET /api/knowledge/search` | Independent KnowledgeStore. The Web page pastes text or Markdown, tags and searches clips, and loads all clips 50 at a time with `limit`/`offset`; new searches and archive toggles restart at offset 0. The original text is stored verbatim with a SHA-256 digest. Create is idempotent per `request_id`, and an unconfirmed save keeps its exact request payload for retry after reload. |
| Recovery review | Task/Run `GET recovery` and `POST reconcile` | Existing checkpoint review and Task sequence or Run revision. Writes require a valid operator session on the same origin or the runtime bearer. |
| Evidence drawer | Persisted Run/Task result and available tool outputs from history | Existing runtime state and Session checkpoint history. The Run drawer scopes evidence to its recorded result. |
| Settings | `/api/runtime/management` and `/api/runtime/commands` | Existing server controls for WeChat, schedules and memory. |

Paths below `/api/runtime/` in the table use that same prefix. Knowledge paths live under `/api/knowledge/` and are covered by the same Web authentication and same-origin mutation checks. Submitted browser requests keep their original IDs for explicit retry after a dropped response. Unsent queue items stay in the tab. The existing page addresses share `src/main-workbench.tsx`. The shell, chat, task, run, approval and knowledge views live under `src/workbench/`. The legacy Server snapshot and task-board API remain available to their existing callers.

Knowledge clips are low-trust reference text. The Web and Agent runtime never read them into a system prompt, conversation history or `MEMORY.md`, and no summarization, OCR, capture or vector search is implemented. The SQLite database is created under the host runtime state directory, outside the Agent workspace, with owner-only permissions.

The former execution-graph page and its React Flow dependency were removed. Old `?view=workflow` links now land on the Development Task list, preserving their `task` parameter. LangGraph execution, Task/Run events, the execution timeline and recovery flows are unchanged.

## Browser verification

Build Web first. Run `env -u TMPDIR uv run python -m tests.support.workbench_browser --channel chrome` against the isolated fixture on macOS, or omit `--channel` after installing Playwright Chromium. On a development host with an unverified SQLite runtime, prefix `CAIRN_ALLOW_UNVERIFIED_SQLITE=1` for this isolated fixture. The fixture starts a Server and real CLI process, forces one crash after a tool effect and verifies the recovery note and untouched artifact after another restart. Optional `--screenshots /absolute/path` saves desktop and narrow captures. The browser fixture creates WeChat-source Runs through the host API and exercises Knowledge paste, search, edit and archive against the real Web bundle. The actual Server receiver with a Fake iLink endpoint is covered by `tests/terminal/test_wechat_e2e_process.py` on Linux.

To exercise the real model path against an isolated provider-configured Server, set `CAIRN_WEB_E2E_URL`, `CAIRN_WEB_E2E_TOKEN_FILE` and `CAIRN_WEB_E2E_REAL_MODEL=1`, then run `pnpm --dir web test:e2e`. This submits one model request.
