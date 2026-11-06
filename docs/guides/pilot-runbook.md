# Operations and recovery

## Environment and private state

Use the checkout's Python environment. Worktree tasks require Git 2.40+; the
SQLite version that matters is the library linked to this Python interpreter:

```sh
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
git --version
uv run python scripts/pilot/preflight.py --profile durable
```

The current SQLite admission policy accepts 3.51.3+, the patched 3.50.7+ branch
and the patched 3.44.6+ branch. Check `src/cairn/infra/sqlite_policy.py` when
upgrading: a system `sqlite3` CLI does not change Python's linked library. An
unverified-development override is not proof of supported storage.

Preflight checks local configuration, shell and storage without a model request.
It does not establish provider, WeChat or publication health. Run it from the
target workspace; read BLOCKED diagnostics before changing host configuration.

Keep Runtime state, policy/approval files, WeChat account state and durable
Session stores outside source and Worktree writable roots. Preserve private
ownership/modes and the Runtime's exclusive lease. Do not put tokens in a prompt
or bypass a live owner by removing its lock. The Runtime creates its state at
0700 and `runtime.token` at 0600.

## Permissions

The host grants capabilities; prompts and tool arguments cannot do so. Shared
Runtime shell access uses the optional Linux project sandbox and fails closed
when isolation is unavailable. Credential paths and Git metadata are masked;
shell network access is shared, not isolated.

The standalone interactive CLI's approved host shell runs as the invoking user
and can reach that user's files and network. A working directory alone is not
an OS sandbox. Configured provider credentials are removed from tool child
environments; keep other secrets outside Agent-readable roots too.

## Interrupted work

A budget pause retains the task, committed history and development Worktree.
`cairn runtime continue` starts a new budget window after the Runtime checks the
durable state. Continue does not silently replay an unfinished tool.

After a crash, cancellation during an effect or storage uncertainty, a task may
require recovery. First inspect task history, the worktree and any external
operation; a tool may have executed before its result was committed.

```sh
uv run cairn-cli runtime inspect TASK_ID --token-file FILE
uv run cairn-cli runtime history TASK_ID --token-file FILE
uv run cairn-cli runtime recovery TASK_ID --token-file FILE > /private/recovery.json
```

Use an existing private directory for the report. After reviewing surviving
effects, explicitly reconcile with the fresh report and a meaningful note:

```sh
uv run cairn-cli runtime reconcile TASK_ID \
  --report-file /private/recovery.json --effects-reviewed \
  --note "Reviewed surviving changes; continue from committed facts" \
  --token-file FILE
uv run cairn-cli runtime continue TASK_ID --token-file FILE
```

Reconcile records `abandon_without_replay`; it runs no model/tool and does not
undo existing effects. Stale reports are rejected; reread and recheck them.
Web exposes the same explicit review and separate Continue action. WeChat can
read recovery facts, but cannot confirm reconciliation.

Local durable CLI sessions use their own commands and default private store
`~/.local/state/cairn/sessions`:

```sh
uv run cairn-cli session list
uv run cairn-cli session inspect SESSION_ID
uv run cairn-cli session reconcile SESSION_ID
uv run cairn-cli session reopen SESSION_ID
```

`--store DIRECTORY` selects a different store. Inspect is read-only; reopen and
reconcile require fresh host confirmation. Follow the exact revision-bound token
shown by the CLI after reviewing effects. Reconciliation exits without running
work; reopen is separate.

## Diagnostics

For shared work, use `runtime inspect/history/follow/recovery` and the server's
logs. A stopped browser or CLI does not imply the Runtime stopped. For standalone
WeChat, use `/wx status` or the [systemd journal](persistent-wechat-service.md).
Preserve task IDs, committed receipts and Worktrees when reporting failures;
redact tokens and credentials. Do not restart unrelated live services to test a
new checkout.
