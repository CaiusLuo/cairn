# SWE-bench Lite internal subset

This is a fixed **internal subset** of SWE-bench Lite plus a thin adapter from a
Cairn diff to the official prediction format. It is deliberate about what it is
not: it is **not** a leaderboard entry and never a claim about full SWE-bench Lite
scores.

Official contract:
[SWE-bench Evaluation guide](https://www.swebench.com/SWE-bench/guides/evaluation/).

## Manifest (frozen)

The subset lives in
[`evaluation/benchmarks/swebench_lite.py`](../../../evaluation/benchmarks/swebench_lite.py)
as `SUBSET` — 12 instances from the official `princeton-nlp/SWE-bench_Lite` test
split, version `swebench-lite-subset-v1`, digest
`fa280cd98c798652c66f2c61a9ac569d231709133e6f565178660823061147e1`.

| Instance | Repo | Version |
| --- | --- | --- |
| astropy__astropy-12907 | astropy/astropy | 4.3 |
| astropy__astropy-14182 | astropy/astropy | 5.1 |
| astropy__astropy-14365 | astropy/astropy | 5.1 |
| astropy__astropy-14995 | astropy/astropy | 5.3 |
| astropy__astropy-6938 | astropy/astropy | unknown (null) |
| astropy__astropy-7746 | astropy/astropy | unknown (null) |
| django__django-10914 | django/django | unknown (null) |
| django__django-10924 | django/django | 3.0 |
| django__django-11001 | django/django | 3.0 |
| sympy__sympy-11400 | sympy/sympy | 1.0 |
| sympy__sympy-11870 | sympy/sympy | 1.1 |
| sympy__sympy-11897 | sympy/sympy | 1.0 |

Base commits are pinned in the module. An unverified dataset version stays
`null` rather than being guessed. Changing any instance, repo, commit or version
changes the digest and requires a new `SUBSET_VERSION`.

## Prediction adapter

`prediction_for_diff(instance_id, model_name_or_path, patch)` and
`write_predictions(...)` emit the official JSONL shape, one object per line, with
exactly `instance_id`, `model_name_or_path` and `model_patch`. An empty diff is a
legal prediction (the harness counts it as an empty patch); the adapter never
invents a change. Instances outside the subset are refused.

Entry point:
[`evaluation/runners/run_swebench_lite.py`](../../../evaluation/runners/run_swebench_lite.py)
(`--check`, `--diffs DIR --predictions out.jsonl`).

## Summary parsing

`summarize_results(official_results_json)` restricts the official `results.json`
(`resolved`, `unresolved`, `error_ids`, `empty_patch_ids`) to the subset and
reports subset counts plus `resolved_rate = resolved / graded` (`None` when
nothing was graded). These are subset counts only.

## Execution contract (what a real run must pin)

A real SWE-bench run is **not** this adapter. Per the official guide, grading
applies each patch and runs the repository's tests inside the official Docker
harness. A Cairn real run must therefore fix, up front:

| Dimension | Requirement |
| --- | --- |
| Model | provider/model id and output cap, recorded per prediction |
| Permissions | explicit, least-privilege; no operator approval is available non-interactively |
| Tools | the exact tool set; shell only if the run explicitly grants it |
| Network | declared; scoring and the Agent must not silently share egress |
| Budget | hard token cap and attempt count, plus wall-clock timeouts |
| Attempts | max Agent steps and repetitions; no automatic retry or expansion |
| Tokens/time | recorded usage (unknown stays null) and elapsed time per instance |
| Isolation | the **Agent** and the **grader** each run in a container or VM; the two must not share a writable host or the same container |

Container/VM isolation is required because the grader applies untrusted
model-authored patches and runs a repository's own test suite, and the Agent may
execute arbitrary shell. A temporary directory plus a timeout is not isolation.

## Two conclusions that must not be generalized

- The Repository Intelligence contract records **no shell** for its own arms.
  That is a fact about **RI's** execution, not about the Cairn CLI or this
  benchmark. A SWE-bench run's shell/network policy is stated for that run and
  is never inherited from RI.
- A subset result is **not** a full SWE-bench Lite score and is **not** a
  leaderboard entry.

## Execution modes and status

The entry distinguishes four modes, each with its own status, so a printed
command is never mistaken for a run:

| Mode | Command | Status | Executes? |
| --- | --- | --- | --- |
| manifest | `--check` | `MANIFEST_OK` | no |
| convert existing patches | `--diffs DIR --predictions P` | `PREDICTIONS_WRITTEN` | no |
| preview command | `--predictions P --print-command` | `COMMAND_READY` | no (`executed=False`) |
| grade | `--predictions P --grade --run-id ID --model M --max-total-tokens N --isolated` | `GRADED` / `LIVE_BLOCKED` | yes — the official harness |

`--grade` actually invokes the official Docker harness
(`python -m swebench.harness.run_evaluation …`) on the predictions and parses its
`logs/evaluation/**/<run_id>/results.json` into subset counts. It requires an
explicit model, hard token budget, `--run-id` and an `--isolated` confirmation;
if the harness is unavailable it reports `LIVE_BLOCKED` with
`missing=["official-harness"]` and applies no patch and runs no test itself. A
non-zero harness exit is reported as `LIVE_BLOCKED` with `executed=true`, not as
a success.

## Status this round: PARTIAL

The full closed loop — **Cairn solve → patch → official grade** — is **partial**.
The *grade* stage is implemented and testable; the *solve* stage (running the
Cairn agent per instance inside an isolated container to produce a patch) is
**not built this round**, so `--live` reports `PARTIAL` with
`missing=["cairn-solve-patch-generation (needs an isolated container agent
runner; not built this round)"]` and never claims a score. No paid model run and
no Docker grading actually executed here (no model/budget authorized, no harness
installed).

```sh
uv run python evaluation/runners/run_swebench_lite.py --check
uv run pytest tests/runtime/evals/test_swebench_lite_adapter.py tests/runtime/evals/test_swebench_lite_entry.py -q
```

A full live run still needs separate authorization naming the model, hard token
budget, attempt/time limits and the isolated container/VM setup, plus the solve
stage above.

## Offline verification

[tests/runtime/evals/test_swebench_lite_adapter.py](../../../tests/runtime/evals/test_swebench_lite_adapter.py)
pins the manifest digest and the official JSONL shape, and
[tests/runtime/evals/test_swebench_lite_entry.py](../../../tests/runtime/evals/test_swebench_lite_entry.py)
proves the entry is offline by default and gates every real-scoring path.
