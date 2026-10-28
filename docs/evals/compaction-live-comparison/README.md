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

The two arms share the same workspace snapshot but may use different context
budgets: `FULL_CONTEXT` gets the full budget (no compaction), while
`REAL_SUMMARY` gets a **tighter** budget so compaction is actually required.
`spec.forbidden_history_marker` and `spec.key_fact_marker` make it possible to
prove that a later request used the *compacted* history and that a key fact
survived; the side-effecting tool appends to a log (not an overwrite), so a
replay is detectable.

## Hard total-token budget (both arms + summarizer)

When `--max-total-tokens` is given, one shared `TokenBudgetLedger` caps
`FULL_CONTEXT`, `REAL_SUMMARY` **and** every summarizer call together. Before each
provider request the harness reserves a conservative upper bound (per-request
prompt estimate + output cap); an unaffordable request is **refused before it is
sent** with `TotalBudgetExceeded`. The reservation is charged, so unknown usage is
never treated as zero. A budget too small to reserve even one request is refused
up front (`LIVE_BLOCKED`) before any provider call. Different context budgets per
arm do not weaken this: they are admission limits, the ledger is the hard cap.

## A valid comparison requires real compaction

A run is a **valid** full-vs-summary comparison only when **all** hold:

- both arms ended within budget;
- the `REAL_SUMMARY` arm actually invoked the summarizer
  (`compaction_occurred`), and a later request carried the current instruction
  without the large older text (`compacted_history_used`);
- the key fact still reached the model after compaction (`key_fact_preserved`);
- a non-idempotent side effect was not replayed (`replayed_side_effect` false).

Otherwise the report sets `valid = false` with `invalid_reasons` — it is **never**
reported as a successful comparison. A summary arm that does not actually compact
is marked invalid rather than passing.

## Reporting contract

- Model, model version, budget, usage, elapsed time and failure class are
  reported; **unknown observations stay null**, never zero.
- **Final workspace correctness** (independent checks) and **within-budget
  termination** are reported **separately**. A run that exhausts its budget does
  not claim a correctness result, and a correct final workspace does not imply a
  clean termination.
- Failure classes: `none`, `run_timeout`, `budget_exhausted`,
  `total_budget_exceeded`, `context_exceeded`, `runtime_error`, `cancelled`,
  `setup_error`.
- Per-arm correctness (`checks_passed`) is reported even when the *other* arm
  overspends, so a correctness result already obtained is never hidden by a
  budget failure; the combined `final_workspace_correct` is only produced when
  both arms terminate.

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
