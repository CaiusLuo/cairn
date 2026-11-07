# P0-A Server Bootstrap handoff

## Work location and review base

Implementation and tests run on Debian 12, SSH alias `My-Debian12`, repository
`/workspace/cairn`, branch `main`. The Mac checkout is not this task's working
copy. Base: `874fa8b2df26fbad27f10f27e69d8548f03b1ffd`.

```sh
cd /workspace/cairn
git status --short --branch
git log --oneline 874fa8b2..HEAD
git diff --stat 874fa8b2..HEAD
git diff 874fa8b2..HEAD
```

Commits are local to Debian until separately published. Inspect current HEAD;
do not reset the branch or overwrite later operator work. GitHub CI is a later
publication/review step. No live provider run, real WeChat send, production unit
installation, operator-service restart or operator-data deletion was performed.

## Code map

| Area | Owning source |
| --- | --- |
| Server composition, preflight, listener, lifespan, CLI options | `src/cairn/server.py` |
| TOML/environment/CLI loading and type validation | `src/cairn/infra/config/server.py` |
| Canonical workspace/data/worktree boundary and component roots | `src/cairn/infra/server_paths.py` |
| Offline import, backup/checksum/integrity and rollback | `src/cairn/infra/state_import.py` |
| Server-owned WeChat/Schedule/Memory controls | `src/cairn/server_controls.py` |
| Remote-only terminal connection and batch Runtime commands | `src/cairn/terminal/client.py`, `src/cairn/terminal/runtime.py` |
| Host-wide WeChat identity lock plus existing serve lock | `src/cairn/infra/wechat/state.py`, `src/cairn/terminal/wechat/serve.py` |
| Protected paths during existing execution | `src/cairn/runtime/service_execution.py` |
| Packaged/source Vite resolution | `src/cairn/web/assets.py`, `web/package-assets.mjs` |
| Debian supervision and five lifecycle actions | `deploy/systemd/cairn.service.in`, `start.sh` |
| Configuration example and operations/storage inventory | `deploy/config.example.toml`, `docs/guides/runtime-service.md` |
| Installed-wheel HTTP acceptance | `scripts/ci/check_server_wheel.py` |

Public aliases and old Web/runtime launcher modules were removed. The standalone
WeChat unit and its obsolete deployment guide were removed. Shared LangGraph,
SessionStore checkpoint/tool-fence/recovery code, Runtime task lifecycle and
Worktree registrations are retained. No PostgreSQL or new permission framework
was introduced.

## Acceptance commands

Use isolated fixture roots. These tests do not need provider API keys or an
operator WeChat account:

```sh
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q tests/server tests/terminal/test_server_process.py tests/terminal/test_server_systemd.py
uv run pytest --cov=cairn --cov-report=term-missing
uv build --wheel
uv run python scripts/ci/check_server_wheel.py dist/cairn-*.whl
```

| Required case | Concrete evidence to inspect |
| --- | --- |
| One command provides Web and API | `tests/server/test_bootstrap.py`: five pages/listener, health and Runtime API; wheel smoke outside checkout |
| CLI exit leaves work alive | `tests/terminal/test_server_process.py`: actual PTY client detachment/reconnection and ongoing task |
| Restart retains state | Real-process task/session/history and token checks; `tests/terminal/test_server_systemd.py` also kills/restarts its unique unit |
| Custom data_root, no hidden XDG state | Bootstrap test poisons legacy defaults; checks Runtime, Session, Schedule, Memory roots and nested writable worktrees |
| Invalid config/occupied port/missing credentials | Bootstrap rejection tests, including malformed TOML, missing provider key, assets, WeChat credentials and damaged Schedule JSON |
| Old data survives import | `tests/server/test_import.py`: real persisted task/history/worktree, hash checks, live-owner rejection and injected rollback failure |
| No duplicate Server/receiver or unintended restart | Server duplicate root admission, cross-process same-account lock, repeated systemd start keeps MainPID, changed roots refuse restart |
| Permissions, tools, recovery and Web unchanged | Full offline regression, shared Runtime tests, WeChat recovery tests and existing SessionStore/tool suites |

The systemd tests use uniquely named `cairn-p0-test-*.service` files under `/run`
and remove them in teardown. They never supervise the operator's `cairn.service`.
Source acceptance does not prove live provider/model acceptance or actual WeChat
QR/login/delivery. Those require a separately selected provider/account and a
bounded live acceptance after review.

## Production cutover after review/CI

1. Review the diff and CI/build results; resolve findings before cutover.
2. Identify every old Server/receiver/supervisor and its actual state/config roots.
   Stop its owning service explicitly. Never unlink live locks or kill by guessed PID.
3. Save private backups and record checksums. Configure the one canonical Server
   root and preserve existing Worktree locations. Move approval/environment policy
   to host-owned configuration if it currently lives under an old state tree.
4. Import selected old component roots with `cairn --config ... import-state`, or
   retain the existing default directories unchanged. Read the verified manifest.
5. Start one new unit. Authenticate Web and CLI, verify old history and recovery
   facts, Schedule jobs/receipts, personal memory, bound WeChat identity and safe
   restart. Do not replay unknown effects to make an acceptance case pass.
6. Complete the old-storage cleanup below only after final operator acceptance.

## Old-storage cleanup gate and inventory

The operator requested full cleanup after review, CI and final acceptance. That
condition has not occurred in this implementation task. Original operator data
and any real migration backups are retained. Do not interpret test success as
permission to delete production data early.

Read-only inventory observed under `/root/.local/state/cairn` on 2026-11-07:

| Existing area | Required disposition after successful cutover |
| --- | --- |
| `runtime/` | Retain if this is the selected active root; otherwise verify/import tasks, Sessions, board, token and ownership facts before deleting the old copy |
| `wechat/` | Verify/import account, cursor/message state and any nested legacy Sessions; retire `background-owner.json` and `background.log` with the old detached supervisor |
| `schedule/` | Verify/import definitions, run/receipt history and interrupted-job facts before removing a stale copy |
| `agent/` | Verify/import rules, memory, profile, daily history and trust records before removing a stale copy |
| `tasks/` | Legacy standalone board; inspect project/revision ownership and archive/import deliberately before deleting. Do not overwrite the active `runtime/board` with it |
| `policy/provider.json` | May still be an active authorization source; first move/verify host policy and update config, then remove the unused original |
| `issue97-evidence/` | Historical acceptance artifacts; remove once final acceptance confirms no retained evidence is needed |

Also inspect any custom `--state-dir`, `CAIRN_AGENT_HOME`, schedule/WeChat roots,
old standalone trace JSONL/indexes, project-memory SQLite, retired unit files and
migration backup locations discovered during cutover. Record canonical paths and
current consumers before deletion. Active directories, external Worktrees, Git
registration, credentials still referenced by config and unrelated project data
are not stale storage.

At final cleanup: stop all old consumers, verify the active Server's authenticated
bootstrap identity against the selected systemd process, compare imported data to
the manifest, and remove only the enumerated inactive originals, retired owner/
log files, obsolete units and then-approved backup copies. Recheck Web/CLI/WeChat/
Schedule and restart after cleanup; the final report must list actual deletions,
retained active paths, final commit and CI/acceptance results. No cleanup automation
or broad recursive deletion against the common root is scheduled by this task.

## Recorded verification before handoff

On Debian 12, the full offline coverage run passed 3,738 tests, skipped 40,
reported 5 warnings and reached 86.19% coverage (required 80%). Focused real
process/systemd/import tests passed before and after the final bootstrap guards.
Ruff, format and Mypy were run against the repository. Web built with the
existing frozen pnpm lockfile. The wheel includes 21 static asset entries and
was installed into a temporary environment outside the checkout; all five Web
pages, their asset URLs, the shared API and graceful shutdown were verified.

Full-suite log: `/tmp/cairn-p0a-full-tests.log`.
Installed-wheel log: `/tmp/cairn-p0a-wheel-check.log`.
These logs are implementation evidence, not authorization to erase old stores.
The follow-up agent should record any later tests against its reviewed final HEAD.
