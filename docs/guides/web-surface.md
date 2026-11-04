# Web surface (W1)

`cairn.web` is an outer presentation adapter parallel to `cairn.terminal`. It
serves the approved two-page browser surface (Sessions and Tasks) over a
loopback ASGI endpoint. It does not own an Agent turn, a Task lifecycle or a
store; one explicit task status move is exposed through the shared TaskBoard
service.

## Start

```bash
export CAIRN_WEB_PASSWORD='set-in-operator-shell'
uv run cairn-web --workspace /workspace/cairn-web \
  --sessions-dir /root/.local/state/cairn/wechat/sessions \
  --tasks-dir /root/.local/state/cairn/tasks \
  --host 127.0.0.1 --port 8766 \
  --web-root /workspace/cairn-web/web/dist
```

`--workspace` is required. The host selects the Session and TaskBoard roots at
startup; browser requests cannot choose arbitrary paths. `cairn-web` serves the
built `web/dist` directory by default. For frontend development:

```bash
cd web
pnpm install
pnpm dev  # Vite on 127.0.0.1:5173; /api proxies to cairn-web on 8766
```

## HTTP and WebSocket contract

All routes stay loopback-only behind Host/Origin guards (no wildcard CORS and no
credentials in the bundle). The public review entry is an operator-configured
nginx reverse proxy to internal port 8766; nginx listens on 8765 and forwards
cookies and WebSocket upgrades. The app serves `/login.html`; `POST /api/login`
sets an HttpOnly SameSite=Strict session cookie. Unauthenticated page requests
redirect to login and unauthenticated API/WS requests are refused. Ordinary
reads are GETs; the only write is the explicit task-status move below.

- `GET /api/health` — liveness and `read_only: true` (public liveness only).
- `POST /api/login` — `{username, auth_passphrase}`; sets the HttpOnly session cookie.
- `GET /login.html` — login surface; ASGI shark animation is vendored from the MIT
  `stackedlol/ascii-sharks` project.
- `GET /api/snapshot` — versioned Session summaries plus TaskBoard facts.
- `GET /api/sessions/{session_id}` — committed Session history from the
  inspection-mode SessionStore only.
- `PATCH /api/tasks/{task_id}` — human task-board status move with
  `{status, expected_revision}`. It uses the shared TaskBoardStore, rejects
  stale revisions with `409 revision_conflict`, and never starts, stops or
  reports an Agent execution. It returns `execution_unchanged: true`. The
  request must carry the login session cookie; the Python app itself remains
  loopback-only.
- `GET /api/ws` — WebSocket `ready` then `refresh` signals with a sequence and
  generation time. It accepts no commands and advances no lifecycle. Clients
  reconnect with bounded backoff and re-read the REST snapshot after reconnect.

`/api/snapshot` reports `source_status.sessions` and `source_status.tasks` as
`ok`, `missing` or `unavailable`; missing facts are not synthesized. Session
source, live owner, Agent identity and title have no stored fact in the current
domain; source/live/agent are returned as `null` and the UI shows the short id
or unknown rather than inventing them. A TaskBoard status of
`failed` or `cancelled` stays distinct in a Closed column; the approved prototype's
`Review` column has no corresponding core status in W1, so it is not fabricated.
Completed work is never relabelled as review or failed.

## Ownership and boundaries

- `cairn.web` may depend on core, infra and runtime, and must not import
  `cairn.terminal`; the architecture check includes the outer web layer.
- Session history is read with `SessionStore(mode="inspect")`. The adapter never
  opens a writer, takes another CLI/WeChat writer's lock, or sends messages,
  approvals or cancellations.
- TaskBoard facts are read through `TaskBoardStore`. W1 exposes one explicit
  human status move (`PATCH /api/tasks/{task_id}`), guarded by revision CAS. It
  has no Agent execution, delete, create or suggestion endpoints. W2 owns runtime
  commands and approval; W3 owns broader card updates and Agent deletion
  suggestions (still human-decided).
- Stored rows are observations, not live owner observations. `live` and `agent`
  remain `null` unless a future adapter supplies a real fact.
- The ASGI process has one snapshot reader, one sequence per changed signature
  and no background task per disconnected WebSocket.

## Verification

```
uv run pytest tests/web tests/core/test_architecture.py
uv run ruff check src/cairn/web tests/web
uv run ruff format --check src/cairn/web tests/web
uv run mypy src/cairn/web
cd web && pnpm typecheck && pnpm build
```

Browser checks cover desktop/narrow layouts, both themes, search/filter,
session/task navigation and explicit empty/error presentations. Fixture data is
test-only; production data comes from the host-selected roots.
