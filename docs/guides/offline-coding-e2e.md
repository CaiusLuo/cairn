# Offline coding task E2E

Issue [#44](https://github.com/CaiusLuo/cairn/issues/44) has an offline acceptance
path through the real `CodingTaskRunner`, `run_turn`, filesystem tools, permission
policy, native Bash sandbox, Git worktrees and JSONL tracing. The provider alone
is scripted. This proves integration behavior; it does not establish real-model
quality or a durable deployment qualification.

## Reproduce

From the repository root, with the locked development dependencies installed:

```sh
uv run pytest tests/runtime/tasks/test_coding_e2e.py -q --basetemp=/tmp/cairn-issue44-e2e
```

Pytest recreates its `--basetemp` directory. Use a dedicated disposable path.
The test uses [local-shell readiness checks](../../tests/support/sandbox.py);
a host without `/bin/bash` produces SKIP, not evidence of successful execution.
No OS sandbox is involved. No configured provider, API credential, network access
or GitHub publication is required.

## Reused task and oracle

The test selects `single-file-bug-fix` from the existing `coding-smoke` EvalSuite
in [coding_smoke.py](../../evaluation/cases/coding_smoke.py). It uses
that case's original prompt, fixture files and `FileContentEqualsCheck` unchanged.
It does not create a second corpus, eval runner or Agent loop. The isolated task
runs through the existing [CodingTaskRunner](../../src/cairn/runtime/tasks/runner.py);
the selected suite checks then inspect the final workspace independently of the
model's final response. The additional behavioral oracle executes the two
inclusive-count examples from the existing prompt in the real Bash sandbox.

A temporary source repository has distinct staged and unstaged sentinel bytes
and an untracked file. `WorktreeProvider` creates a detached task worktree from
its committed fixture. The scripted model reads the buggy function, attempts a
denied capability request and an escaping file edit, makes the fixture's exact
repair, executes the Python assertions, and returns a complete final response.

The host verifies actual UTF-8 bytes and SHA-256, the expected unified diff, no
unexpected tracked/untracked task files, unchanged task HEAD/index, unchanged
source files/index/HEAD/refs, and removal of the task worktree registration.
The denied command creates no marker; the escaping edit leaves the outside file
unchanged. The synthetic configured secret must be absent from the Bash child
environment, evidence report and trace. No network capability is granted.

## Outcomes and evidence

Seven scenarios cover a verified repair; no approval handler with unavailable
usage; a model failure after effects; cooperative and external cancellation after
effects; an edit that matches no text; and a final success claim without a repair.
The test timeout bounds the run; cancellation also checks that owned
async work settles before cleanup and no task leaks remain.

Each scenario leaves an `evidence/` directory under its pytest temporary directory:

| Artifact | Meaning |
| --- | --- |
| `report.json` | Contract `issue-44-offline-v1`, harness revision, fixture HEAD, suite/case, TaskResult when returned, cancellation status, trace ID, elapsed time, usage, tool results and deterministic checks |
| `traces/*.jsonl` | Real runtime spans for model attempts, preflight, permissions, tool execution and finalized Turn |
| `actual.bin`, `expected.bin` | Observed final file bytes and the original smoke oracle's desired bytes |
| `actual.patch`, `expected.patch` | Observed patch and independently derived patch for that scenario's expected final state |

`TaskStatus.COMPLETED` means normal runtime termination, not verified correctness.
Failed editing and false claims leave checks failed. Conversely, a model failure
or cancellation after completed edits can leave correct final files. Reports keep
these facts separate. External cancellation propagates without fabricating a
TaskResult; the report retains the real trace and file observations.

Successful scripted responses supply synthetic usage to exercise runtime
aggregation. Missing usage and failed/cancelled model requests remain null rather
than becoming zero. Cost, Session ID and Turn ID remain null for this stateless
scripted path. Model/tool counts describe only this script and are not quality
or production-cost measurements.

## Real provider stage

**LIVE_BLOCKED**: no real provider is invoked by this command. A live run requires
separate owner authorization identifying the provider/model and token/cost cap.
Its actual diff, deterministic checks, usage availability, elapsed time and trace
must be recorded independently. Offline success cannot satisfy that acceptance.
No merge, automatic retry or remote publication belongs to this offline check.
