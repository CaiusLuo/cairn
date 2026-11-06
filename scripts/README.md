# Operational scripts

Run from the repository root after `uv sync --locked --all-groups`.

- [pilot/preflight.py](pilot/preflight.py) checks local configuration, shell and
  storage offline. See [operations](../docs/guides/pilot-runbook.md) for storage
  admission and recovery.
- [github/run_issue_task.py](github/run_issue_task.py) runs an explicitly approved
  GitHub Issue workflow. It can execute a model, push a branch and open a Draft
  PR; use only for a repository and publication scope the operator authorized.
  Its CLI describes provider, workspace, verification and credential options.

```sh
uv run python scripts/pilot/preflight.py --help
uv run python scripts/github/run_issue_task.py --help
```

Help does not execute a workflow. Keep credentials in host configuration; do not
include tokens in command text or reports.
