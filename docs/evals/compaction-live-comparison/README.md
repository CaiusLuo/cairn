# Real-model compaction comparison (opt-in)

This describes the opt-in, real-provider counterpart to the **offline**
compaction suite ([context_compaction.py](../../../evaluation/cases/context_compaction.py)).
The offline suite stays the deterministic regression for Cairn's compaction
*mechanism*; this contract covers the separate question only a real model can
answer.

## What is compared

For one fixed continuation task, two arms start from the **same pre-compaction
snapshot** in **independent throwaway workspaces** and continue through the one
lifecycle owner, `runtime.loop.run_turn`:

| Arm | Continuation context |
| --- | --- |
| `FULL_CONTEXT` | the complete context, uncompacted (baseline) |
| `REAL_SUMMARY` | older history replaced by a real summary from the session's own model |

The comparison is only meaningful when **both** contexts fit the budget. The
harness then checks that, in either arm:

- the current instruction still reaches the model verbatim;
- executed tool facts are preserved and a side effect already applied in the
  snapshot is **not replayed**;
- final workspace behaviour (independent deterministic checks) is correct.

## Reporting contract

- Model, model version, budget, usage, elapsed time and failure class are
  reported; **unknown observations stay null**, never zero.
- **Final workspace correctness** (independent checks) and **within-budget
  termination** are reported **separately**. A run that exhausts its budget does
  not claim a correctness result, and a correct final workspace does not imply a
  clean termination.
- Failure classes: `none`, `run_timeout`, `budget_exhausted`,
  `context_exceeded`, `runtime_error`, `cancelled`, `setup_error`.

Implementation: [`src/cairn/runtime/evals/compaction_ab.py`](../../../src/cairn/runtime/evals/compaction_ab.py).
Entry point: [`evaluation/runners/run_compaction_live.py`](../../../evaluation/runners/run_compaction_live.py).

## Status: LIVE_NOT_RUN

No model and no total budget were authorized for this round, so no paid
experiment was started. The entry point defaults to `LIVE_NOT_RUN`: it writes a
skeleton report with null observations and spends nothing. A real run requires
**all** of:

```sh
uv run python evaluation/runners/run_compaction_live.py \
  --report .cairn/compaction-live.json \
  --live --model <provider/model-id> --max-total-tokens <hard-cap>
```

`--help` is offline. Without `--live`, or without both `--model` and
`--max-total-tokens`, the command refuses to call a provider.

## Orchestration verified offline

The comparison wiring, arm isolation, snapshot fidelity, fact preservation,
side-effect-once behaviour and termination classification are verified with
scripted doubles in
[`tests/runtime/evals/test_compaction_ab.py`](../../../tests/runtime/evals/test_compaction_ab.py)
and [`tests/runtime/evals/test_compaction_live_entry.py`](../../../tests/runtime/evals/test_compaction_live_entry.py).
Offline doubles establish harness behaviour, **never** real-model improvement.

## Frozen regression

The offline scripted compaction assets are unchanged by this contract and remain
the mechanism regression:

- [`evaluation/cases/context_compaction.py`](../../../evaluation/cases/context_compaction.py)
- [`evaluation/runners/run_context_compaction.py`](../../../evaluation/runners/run_context_compaction.py)
- [`tests/runtime/evals/test_context_compaction_cases.py`](../../../tests/runtime/evals/test_context_compaction_cases.py)
