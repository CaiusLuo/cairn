# Pilot observations: partial real-provider experiment

**REAL_EVAL_BLOCKED.** The fixed pilot planned 24 runs. It stopped after 13 attempted runs because the first model request of `find-label-normalizer / A` failed. Twelve runs received responses and completed Agent execution/checking. The failed request's underlying cause was not retained by runtime `91e2208`; do not infer authentication, quota, malformed response or outage. No further provider calls or fake substitute runs were made.

[Machine-readable experiment](experiment.json), [per-run records](runs/), [comparison](comparison.json), [derived findings](findings.md), and [sanitized trace IDs/counts/timing](traces/) are retained. Runtime code was `91e2208c2d7a6eab54f952378be6da8dfb233362`; the later reporting refinement separates budget completion from workspace correctness and computes balanced cohorts. Original per-run records and the frozen dataset remain unchanged.

Model: `openai/qwen3.8-27b`, configured provider `qwen`. One repetition; seed `230028`; eight steps/run; 16,384 context tokens with 2,048 response reserve; 120s/run, 5s/check and 10s/Bash. All feature schema/context overhead was counted. There were **95 model generation attempts and 94 returned responses**. Twelve run usage observations sum to **353,119 provider input tokens and 28,395 output tokens**; failed-request usage is unavailable. No dollar cost is inferred. LiteLLM counter flags in this pilot were `is_estimate=false`; counter values remain separate from actual provider usage and are not a provider billing guarantee.

Only three case/repetition groups have all four arms: quote-dependencies, testing-convention and shared-account-contract. Compare these 12 matched runs; the unpaired failed A run must not inflate apparent feature benefit.

| Matched observation | A Baseline | B Graph | C Memory | D Both |
| --- | ---: | ---: | ---: | ---: |
| EvalRunner PASS within budget | 1/3 | 2/3 | 1/3 | 1/3 |
| All final workspace checks passed | 3/3 | 3/3 | 3/3 | 3/3 |
| Total tool attempts | 36 | 39 | 35 | 38 |
| read_file calls | 12 | 13 | 11 | 11 |
| Explicit Bash discovery calls | 9 | 9 | 10 | 5 |
| Graph queries | 0 | 4 | 0 | 8 |
| Provider input tokens | 58,856 | 105,887 | 81,725 | 106,651 |
| Provider output tokens | 7,242 | 7,905 | 6,640 | 6,608 |
| Memory inclusion events | 0 | 0 | 19 | 19 |

Seven of the 12 matched runs ended with `budget_exhausted` despite passing every final workspace check. The existing EvalRunner correctly reports these as ERROR. The higher B completion count is an observation within this step budget, not evidence of better final-code correctness: deterministic workspace results were identical in this cohort.

Memory had one fewer total tool attempt/read than Baseline across three cases, with more provider input tokens. Both reduced classified Bash discovery calls but added eight Graph calls and increased total tool attempts. Graph increased total tool/read calls. These mixed results do not establish a net retrieval-efficiency improvement. They also do not establish a correctness improvement. One repetition and an incomplete six-case pilot support no statistical or general performance claim.

The original stale/unknown-fixture and unrelated-memory negative-control cases were **not reached in real execution**. Their full four-arm Git/Memory/request isolation is covered by deterministic offline tests; that is harness correctness, not a real-provider quality result. Included memory precision was 1.0 for C/D on the reached relevant fixtures; this narrow fixture ground truth does not establish general retrieval precision. Freshness rechecks also excluded memories after relevant Agent edits; detailed counters are in the JSON.

Timing limitation: the first D quote run overlapped a local full pytest process until that process was interrupted. Later pilot runs executed without that suite; the full suite was restarted after the pilot stopped. Keep measured elapsed values as observations, but do not claim controlled wall-time speed improvements from this pilot. Provider temporal variation and unchanged sampling defaults are additional limitations.

No case, prompt, solution oracle, feature behavior or budget was tuned after observed failures. Post-pilot changes improve failure categorization, path-safe artifact names, missing-response timeout stopping and report interpretation only. They have offline regressions and need independent code review, as does the experiment as a whole.

Next steps after review and provider availability: diagnose the failed-request category with bounded sanitized metadata; authorize a new separately versioned run record at the same frozen dataset/controls; complete the missing cases and use multiple interleaved repetitions. Study Graph adoption and completion/verification step overhead before considering broader repositories or production enablement. Preserve the current default tool registry and Memory approval authority.
