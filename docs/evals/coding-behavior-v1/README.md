# Coding behavior v1 contract (`graded-behavior-v1`)

This is the versioned **behavioral** acceptance contract for the five coding-smoke
prompts. It is additive: it does not replace or reinterpret the frozen
exact-content corpus [`coding-smoke`](../../../evaluation/cases/coding_smoke.py),
which stays the reference for "smallest coherent change" tasks. A changed
acceptance oracle needs its own version string, so the behavior checks live in a
separate module with a pinned dataset digest.

Implemented in
[`evaluation/cases/coding_smoke_behavior.py`](../../../evaluation/cases/coding_smoke_behavior.py).
Current version `graded-behavior-v1`, digest
`70626544b76834685f1774b4873f9159c75747e01392762a2268a81e451bf7eb`
(see `behavior_dataset_digest`; a test pins it).

## Why a second oracle

The same five prompts are graded two ways for two different risks:

| Risk | Exact-content corpus | Behavior corpus |
| --- | --- | --- |
| A different-but-correct implementation is rejected | possible | avoided: exercises public functions, not bytes |
| A near-miss that fixes the shown example but breaks a boundary passes | possible for non-exact cases | avoided: asserts the exact boundaries |
| A correct target buried in unrelated rewrites passes | avoided by `FileContentEqualsCheck` | avoided by protected-file + behavior-of-old-function checks |

Neither oracle replaces the other. `coding-smoke` remains the byte-exact
"smallest change" gate; `graded-behavior-v1` adds behavior-and-boundary grading
and a different-implementation tolerance.

## Contract

`BehaviorCheck` ([`src/cairn/runtime/evals/behavior.py`](../../../src/cairn/runtime/evals/behavior.py))
executes a host-owned Python script through the real Bash tool and turns the exit
code into a verdict. It scores observable behavior:

* **Inputs/outputs and boundaries** — each script imports the module under test
  and asserts the exact examples named in the prompt plus adjacent boundary cases
  (equal endpoints, empty input, negative start, exhausted retries).
* **Old functionality** — cases that must preserve unrelated behaviour
  (`format_user_id`, `DEFAULT_TIMEOUT_SECONDS`, retry success semantics) assert it,
  so a fix that damages an untouched function fails.
* **Protected files** — where a case must not rewrite an unrelated file, an
  exact-content check pins it; `behavior_protected_files` exposes that set.

### Execution and isolation (honest scope)

The script runs with the workspace interpreter through the real `BashTool`: a
fresh throwaway temporary workspace, declared provider-secret variables stripped,
a bounded wall-clock timeout and bounded output. **This is not an OS sandbox.** A
timeout plus a temporary directory is not isolation of adversarial code. The
scripts are host-authored trusted fixtures — never model context, never written
by the evaluated model, never persisted into the case. The evaluated *generated
code* runs inside the throwaway case workspace whose lifetime `EvalRunner` owns;
that containment, plus the timeout, is all this contract claims.

### Versioning

- `BEHAVIOR_DATASET_VERSION = "graded-behavior-v1"` and the digest pin every
  prompt, behavior script, protected entry and check. Changing any of them
  requires a new version string and a new digest constant.
- The frozen Repository Intelligence V1/V2 datasets and their 2026-10-11 pilot
  evidence are untouched by this contract.

## Evidence

Offline tests
([`tests/runtime/evals/test_coding_behavior_cases.py`](../../../tests/runtime/evals/test_coding_behavior_cases.py))
drive the real `EvalRunner` → `run_turn` → filesystem-tools path with scripted
models and prove:

* initial buggy fixture fails (except the no-op case);
* a reference **and** a different-but-correct solution pass (no false negative);
* a near-miss fails (no false positive);
* a correct target that damages a preserved function fails;
* grader execution stays inside the throwaway workspace;
* the digest/version gate.

`BehaviorCheck` unit coverage lives in
[`tests/runtime/evals/test_behavior_check.py`](../../../tests/runtime/evals/test_behavior_check.py).

## Running

Offline validation (default):

```sh
uv run pytest tests/runtime/evals/test_behavior_check.py tests/runtime/evals/test_coding_behavior_cases.py -q
```

Real-provider run (explicit authorization required; `--help` is offline):

```sh
uv run python evaluation/runners/run_coding_behavior.py --report .cairn/behavior-report.json
```

## Limits

Offline scripted runs prove grader discrimination and isolation wiring, **not**
model quality. A real-provider run needs separate authorization naming the
provider/model and budget; its results are descriptive, not a leaderboard. The
grader is not a sandbox (see above).
