# Evaluation tools

Run from the repository root after `uv sync --locked --all-groups`.

| Location | Contents |
| --- | --- |
| `cases/` | Coding fixtures, prompts and deterministic acceptance checks |
| `runners/` | Explicit evaluation entry points |
| `benchmarks/` | Offline performance probes |
| `langgraph_spike.py` | Compatibility probe against the production Agent turn |

Reusable datasets and orchestration also live in `src/cairn/runtime/evals/`.
The Repository Intelligence dataset is built by
`src/cairn/runtime/evals/intelligence/dataset.py`; it does not read old pilot
reports from `docs/`. Fixture and oracle behavior is exercised by the tests.

Offline entry points:

```sh
uv run python -m evaluation.langgraph_spike
uv run python -m evaluation.benchmarks.session_checkpoints --help
```

The coding and intelligence runners can call paid providers. Inspect `--help`
offline and obtain authorization before a live run:

```sh
uv run python evaluation/runners/run_coding_smoke.py --help
uv run python evaluation/runners/run_coding_behavior.py --help
uv run python evaluation/runners/run_repository_intelligence.py --help
uv run python evaluation/runners/run_compaction_live.py --help
```

`cases/coding_smoke_behavior.py` contains the `graded-behavior-v2` checks.
`run_compaction_live.py` requires `--live`, a model and a total-token budget for
live execution. Offline doubles prove mechanics, not real-model improvement.
Operational checks and publication tooling are indexed in [scripts](../scripts/README.md).
