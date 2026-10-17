# Operational scripts

These repository entry points operate Cairn; evaluation cases and experiments
belong in [evaluation/](../evaluation/README.md).

- [pilot/preflight.py](pilot/preflight.py) checks local configuration, sandbox and
  storage admission offline. Follow the [pilot runbook](../docs/guides/pilot-runbook.md).
- [github/run_issue_task.py](github/run_issue_task.py) runs the explicitly approved
  disposable-repository workflow. Follow the
  [GitHub smoke preflight](../docs/guides/github-smoke-preflight.md) before access
  or publication; help does not authorize either.

Run from the repository root after `uv sync --locked --all-groups`:

```bash
uv run python scripts/pilot/preflight.py --help
uv run python scripts/github/run_issue_task.py --help
```

These scripts are repository tooling, separate from the installed `cairn` CLI.
