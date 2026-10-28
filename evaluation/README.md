# Evaluation assets

Run repository evaluations from the repository root after installing development
dependencies with `uv sync --locked --all-groups`.

| Directory | Owner |
| --- | --- |
| `cases/` | Repository-specific fixtures and deterministic acceptance checks |
| `runners/` | Explicit executable experiments using the reusable runtime APIs |
| `benchmarks/` | Offline performance probes |

Reusable evaluation orchestration, dataset contracts and reporting remain in
`src/cairn/runtime/evals/`. Operational checks and publication scripts live in
[scripts/](../scripts/README.md).

The coding smoke and Repository Intelligence runners use real providers. Running
them requires separate authorization; `--help` is offline:

```bash
uv run python evaluation/runners/run_coding_smoke.py --help
uv run python evaluation/runners/run_coding_behavior.py --help
uv run python evaluation/runners/run_repository_intelligence.py --help
uv run python -m evaluation.benchmarks.session_checkpoints --help
```

`cases/coding_smoke_behavior.py` is the versioned `graded-behavior-v1` behavioral
acceptance set for the five coding-smoke prompts; its contract is
[coding behavior v1](../docs/evals/coding-behavior-v1/README.md).

The [offline coding E2E](../docs/guides/offline-coding-e2e.md) uses the same smoke
cases with scripted responses. The
[checkpoint benchmark guide](../docs/performance/session-checkpoints.md) describes
the offline performance probe.

[Repository Intelligence V1](../docs/evals/repository-intelligence-v1/README.md)
and [V2](../docs/evals/repository-intelligence-v2/README.md) retain their original
contracts and historical evidence. Historical commands in those records describe
the checkout used at the time; the current Repository Intelligence entry point
is `evaluation/runners/run_repository_intelligence.py`. Moving repository assets
does not change fixtures, prompts, dataset digests or acceptance checks.
