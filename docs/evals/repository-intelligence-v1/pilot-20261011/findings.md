# Repository intelligence pilot

Execution: **real**; state: **real_eval_blocked**; real-provider status: **BLOCKED**.
Model: `openai/qwen3.8-27b`; provider: `qwen`; seed: `230028`; repetitions: 1.
Dataset: `repo-intelligence-v1` / `b459ce3fd1293e540572a2ebf8ae0c4a10407fd28a4be76f117a776d45e7553f`; code: `91e2208c2d7a6eab54f952378be6da8dfb233362`.
Budget: 8 steps/run, 16384 request tokens including 2048 response reserve; run/check/tool timeouts 120.0/5.0/10.0s.
Sandbox: macos-sandbox-exec; network: baseline-deny-no-approval.

| Arm | Runs | PASS / FAIL / ERROR | Success | Mean tools | Mean reads | Mean Bash discovery | Mean Graph queries | Provider input / output totals | Request-count total | Mean seconds |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| A | 4 | 1 / 0 / 3 | 0.25 | 9 | 3 | 2.25 | 0 | 58856 / 7242 | 46504 | 42.59 |
| B | 3 | 2 / 0 / 1 | 0.67 | 13 | 4.33 | 3 | 1.33 | 105887 / 7905 | 79321 | 60.39 |
| C | 3 | 1 / 0 / 2 | 0.33 | 11.67 | 3.67 | 3.33 | 0 | 81725 / 6640 | 65284 | 55.59 |
| D | 3 | 1 / 0 / 2 | 0.33 | 12.67 | 3.67 | 1.67 | 2.67 | 106651 / 6608 | 79492 | 56.50 |

All-observed aggregates above can be unbalanced after interruption. The matched comparison below uses only case/repetition groups observed in all four arms.

| Matched arm | Runs | Overall PASS | Workspace checks PASS | Mean tools | Mean reads |
| --- | ---: | ---: | ---: | ---: | ---: |
| A | 3 | 1 | 3 / 3 | 12 | 4 |
| B | 3 | 2 | 3 / 3 | 13 | 4.33 |
| C | 3 | 1 | 3 / 3 | 11.67 | 3.67 |
| D | 3 | 1 | 3 / 3 | 12.67 | 3.67 |

| Case / repetition | Arm | Verdict / category | Workspace checks | Failed checks | Trace |
| --- | --- | --- | --- | --- | --- |
| quote-dependencies / 1 | D | ERROR / budget_exhausted | PASS | none | `155527881061420ba5fbf1fe4041d417` |
| quote-dependencies / 1 | B | PASS / none | PASS | none | `317a82754b6346d08e1c287d1c290fa7` |
| quote-dependencies / 1 | A | PASS / none | PASS | none | `871be3b10a054a448c922b9eea5f7a4f` |
| quote-dependencies / 1 | C | PASS / none | PASS | none | `88aa036f70f74f9dbd107431236ab65a` |
| testing-convention / 1 | C | ERROR / budget_exhausted | PASS | none | `d1553c7d1ca44917a7e7b27de48b7ab7` |
| testing-convention / 1 | B | PASS / none | PASS | none | `53285a4d8c07422b991a35af25dd2dea` |
| testing-convention / 1 | A | ERROR / budget_exhausted | PASS | none | `82d6c76764ee4190a5c30e40d6aa8a8e` |
| testing-convention / 1 | D | PASS / none | PASS | none | `33ac01d5fadb4f9a84133053183ea9e1` |
| shared-account-contract / 1 | A | ERROR / budget_exhausted | PASS | none | `0816f0b746534bccbb6f9032e931a230` |
| shared-account-contract / 1 | C | ERROR / budget_exhausted | PASS | none | `ade6f659e5b645edbb759c1d6c5b684c` |
| shared-account-contract / 1 | D | ERROR / budget_exhausted | PASS | none | `dae36ad81f184d17acefe0b836e25615` |
| shared-account-contract / 1 | B | ERROR / budget_exhausted | PASS | none | `063ef9e5f36c45dab74d51137f348059` |
| find-label-normalizer / 1 | A | ERROR / runtime_error | FAIL | find-label-normalizer:behavior | `8c6fc2b785514410ba5098cc57adb896` |

| Case / repetition | Arm vs A | PASS delta | Tool delta | Read delta | Request-count delta |
| --- | --- | ---: | ---: | ---: | ---: |
| quote-dependencies / 1 | D | -1 | -1 | -1 | 6503 |
| quote-dependencies / 1 | B | 0 | 0 | -2 | 10145 |
| quote-dependencies / 1 | C | 0 | -1 | -2 | 1159 |
| testing-convention / 1 | C | 0 | 0 | 1 | 10212 |
| testing-convention / 1 | B | 1 | 1 | 1 | 14159 |
| testing-convention / 1 | D | 1 | -2 | -1 | 12858 |
| shared-account-contract / 1 | C | 0 | 0 | 0 | 8051 |
| shared-account-contract / 1 | D | 0 | 5 | 1 | 14269 |
| shared-account-contract / 1 | B | 0 | 2 | 2 | 9155 |

Observed differences are descriptive. Faster retrieval is not evidence of higher task success unless deterministic PASS results improve. No case was tuned after observing pilot results.

Assumptions and limitations:

- Synthetic Python AST repositories; results do not establish general repository or production quality.
- Offline fake-LLM tests validate the harness only. One repetition establishes no statistical significance.
- Provider sampling defaults are unchanged; seeded scheduling controls order, not model randomness.
- Request counts include full serialized tool schemas and admitted memory. Provider usage is separate; no dollar cost is inferred.
- Discovery evidence means a ground-truth relevant path was returned by read_file or repo_graph, not that the model understood it. Bash discovery and repeats are unavailable beyond explicit utility/read-file classifiers.
- Bash discovery counts calls containing rg/grep/find/ls/tree tokens; this heuristic is not complete semantic search intent.
- Memory counts are retrieval/admission events and can count an entry repeatedly. Stale recall denominator is observed non-fresh candidates; no eligible denominator means null.
- Memory fixtures are human-authorized, host-allowlisted facts, with exact independent source checks; arbitrary statement truth still requires human judgment.
- Tool sandbox is the existing OS backend with network denied and no approval handler. Host Git/Memory setup uses trusted code, outside evaluated model authority.
- End-to-end timing includes fixture setup, deterministic checks and cleanup; Agent timing and feature/setup overhead are separate. Memory timing includes an extra bounded measurement freshness check.
- Actual provider usage may be partial after provider failure; request totals count constructed requests, even if transport fails. Completed responses are recorded separately.
- EvalRunner reports ERROR on budget exhaustion even when every workspace check passes. Overall task-success and final-workspace check success are reported separately; finishing within the step budget is part of the former.
- Traces retain IDs/counts/timings only. Model text, tool output, check scripts, raw exceptions and credentials are excluded.
