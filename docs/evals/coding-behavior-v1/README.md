# Coding behavior v1 contract (`graded-behavior-v1`)

This is the versioned **behavioral** acceptance contract for the five coding-smoke
prompts. It is additive: it does not replace or reinterpret the frozen
exact-content corpus [`coding-smoke`](../../../evaluation/cases/coding_smoke.py),
which stays the reference for "smallest coherent change" tasks. A changed
acceptance oracle needs its own version string, so the behavior checks live in a
separate module with a pinned dataset digest.

Implemented in
[`evaluation/cases/coding_smoke_behavior.py`](../../../evaluation/cases/coding_smoke_behavior.py).
Current version `graded-behavior-v2`, digest
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

### Execution isolation

The behavior script is host-owned trusted code, but it **imports the code the
evaluated model wrote**, so that generated code is untrusted. It is therefore run
inside a real sandbox, [`cairn.infra.sandbox`](../../../src/cairn/infra/sandbox.py),
not a plain subprocess:

- bubblewrap (`bwrap`) with read-only system mounts for **code and libraries only**
  (`/usr`, `/lib`, `/lib64`, `/bin`). `/etc`, `/sbin`, `/home`, `/root` and the
  repository are **not** mounted: read-only is not confidential, so mounting
  `/etc` would expose `/etc/passwd`, `/etc/shadow` and host config to generated
  code. A workspace symlink to a host path resolves to a non-existent target;
- the case workspace bind-mounted read-write at its own path, plus a private
  `/tmp`;
- `--unshare-all` (private user/pid/mount/net/ipc/uts/cgroup namespaces) — no
  network, fresh `/proc`, no host session;
- a cleared environment (only `PATH`, `HOME`) and in-process resource limits
  (`RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE`, `RLIMIT_CORE`).

A temporary directory plus a timeout is **not** a sandbox. When no sandbox is
available (no `bwrap`/user namespaces), `BehaviorCheck` fails explicitly with
`error = "IsolationUnavailable: …"` (the case is an ERROR) and **never falls back
to running generated code on the host**. The real-provider behavior runner also
refuses to start when isolation is unavailable. The sandbox never broadens host
permissions or weakens an existing check.

Offline tests prove the boundary: generated code cannot read an injected host
secret file or environment value, cannot overwrite a host marker, cannot reach the
network, and a missing sandbox fails instead of executing. See
[`tests/runtime/evals/test_behavior_check.py`](../../../tests/runtime/evals/test_behavior_check.py).

### Trusted host-side verdict (v2)

The verdict is computed on the **host**, never inside the sandbox. A case supplies
a *trusted probe program* that records raw observations with `obs(name, value)`
and a host-side `expected` map (literal values, or `Contains` for a substring).
The probe runs isolated and writes a bounded JSON observation record; the host
then compares it to the expectations and only then marks PASS. Because the
expected values never enter the sandbox, they cannot be read or forged.

Consequences: a candidate that calls `os._exit(0)`, `sys.exit(0)`, prints a fake
`SUCCESS` line, raises, or otherwise does not complete produces **no complete
observation record** and FAILS — exit status is never a pass signal. A candidate
that does return the right observations is correct by behaviour (hardcoding the
right answers counts as correct behaviour, a known behavioural-testing limit).
`sys.exit`/raised exceptions are recorded as an incomplete probe; `os._exit`
bypasses the record entirely.

The sandbox streams and caps stdout/stderr (no full buffering in the parent), and a
timeout or external cancellation kills the whole process group and reaps readers
before the error propagates — repeated cancellation cannot leave the child writing
the workspace.

### Versioning

- `BEHAVIOR_DATASET_VERSION = "graded-behavior-v2"` and the digest pin every
  prompt, probe program, expected-observation map, protected entry and check.
  Changing any of them requires a new version string and a new digest constant.
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
