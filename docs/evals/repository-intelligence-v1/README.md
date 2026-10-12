# Repository intelligence v1 experiment

Tracks [#28](https://github.com/CaiusLuo/cairn/issues/28), following [EvalSuite #19](https://github.com/CaiusLuo/cairn/issues/19), [RepoGraph #22](https://github.com/CaiusLuo/cairn/issues/22) and [Project Memory #23](https://github.com/CaiusLuo/cairn/issues/23).

This optional experiment measures deterministic final repository correctness and observed retrieval/resource behavior. It does not activate repository tools or memory approval in normal Agents. The original five coding smoke cases and their checks are unchanged.

## Frozen dataset and controls

Dataset `repo-intelligence-v1` lives in `src/cairn/runtime/evals/intelligence/dataset.py`. Its SHA256 is `b459ce3fd1293e540572a2ebf8ae0c4a10407fd28a4be76f117a776d45e7553f`. Offline tests pin this digest. Any subsequent task or check revision needs a new dataset version; never tune after observing results.

Each case starts with 33 small Python/service/documentation files, including decoys. The host applies exactly the same contents, fixed Git commits and current tree in all arms. A new temporary repository/project identity is created for every case, arm and repetition. MemoryStore directories are also unique and outside all model-writable roots. Stores and repositories are deleted after checks or cancellation. Project identity deliberately differs between arms; initial tree hashes must match.

| Case | Discovery/task contract | Memory fixture | Deterministic oracle |
| --- | --- | --- | --- |
| quote-dependencies | Find catalog/checkout dependencies and repair tax versus shipping contract | Existing module exports/import relationships | Quote boundary/expedited amounts and unrelated-file preservation |
| find-label-normalizer | Locate render_tag's helper without a target path | Existing import and contract documentation location | ASCII whitespace, Unicode casing and tag prefix |
| shared-account-contract | Locate consumers and preserve shared normalization contract | Established shared export | Both return shapes, Unicode/whitespace and normalizer signature |
| testing-convention | Reject blank identities and add a regression using existing convention | Independently verified unittest discovery command | Actual discovered tests pass, then detect a host-side permissive-normalizer mutant |
| stale-batch-history | Respect current configuration after it changed from 50 to 20 | Historical stale fact plus incomplete/unknown fact | Current batch sizes, dynamic configuration and preserved order |
| unrelated-memory-control | Fix a median reducer | Unrelated account fact | Empty/odd/even medians and preserved input |

Task prompts name behaviors or public symbols, not target filenames or expected patches. Host check scripts/reference answers are never model context. Fixture statements describe existing engineering facts and exclude target solutions. Each memory acceptance uses MemoryService/EvidenceResolver and independently verifies exact source files before the model is instantiated. The benchmark-only approval allowlist binds the exact Candidate, CODE origin, source HEAD and verified Git tree. Incomplete claims remain accepted historical entries but ineligible for injection.

The four arms are A (three default tools), B (plus graph query), C (plus approved MemoryReader) and D (both). Model identifier/endpoint, output cap, prompts, RunBudget, ContextBudget, timeouts, network/permission policy and contents are fixed. A recorded seed shuffles cases then arms inside each case/repetition. Provider sampling defaults remain unchanged. There is no model fallback in the pilot.

## Adapter boundaries

EvalRunner adds only two protected lifecycle hooks for fixture preparation and Agent construction; default behavior is unchanged. The dedicated IntelligenceRunner owns experimental state. It uses the normal ContextBuilder/ModelExecutor path, which counts additional tool schemas and optionally admitted memory in the complete request without mutating AgentState or granting authority.

`repo_graph` exposes only list_files, find_symbols, module_for_path and dependencies_of. It validates bounded arguments (1-20 items) and returns at most 8 KiB. Every call rebuilds a bounded snapshot and uses the returned candidate; an incomplete rebuild returns no facts even if the core builder retains an older complete snapshot. Query-level truncation/unsupported flags remain visible. No shell execution, writes or privilege grants occur through this tool. Resource limits bound synchronous AST work; cancellation is observed before/after rebuilding, not inside an individual AST parse.

Git preparation is trusted host code and uses the existing bounded Git subprocess helper. Model Bash commands retain the OS sandbox and baseline permissions with no approval handler, including network denial. On macOS the backend is sandbox-exec; on Linux it is Bubblewrap. Verification subprocesses strip declared provider credential variables. OS sandbox and same-user host integrity remain security assumptions.

## Running and artifacts

Run offline validation first:

```sh
uv run pytest tests/runtime/evals -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest --cov=cairn --cov-report=term-missing
```

A real provider pilot is explicit and finite:

```sh
uv run python examples/evals/run_repository_intelligence.py --report-dir .cairn/intelligence-pilot-v1
```

The runner now defaults to the current `repo-intelligence-v2` contract; the v1 contract and its 2026-10-11 pilot results are frozen. Any new provider run must use a new output directory — see [repository-intelligence-v2](../repository-intelligence-v2/README.md).

Invocation authorizes 24 runs (6 cases x 4 arms x 1 repetition), at most 8 Agent generations per run, 16,384 context tokens including 2,048 response reserve, 120s/run, 5s/check and 10s/Bash tool call. This caps Agent generation attempts at 192; provider-internal retry/billing semantics are not independently observable. The run stops on a model request exception instead of trying further paid calls. No automatic expansion occurs. `--repetitions` accepts 1-3 for a separately authorized rerun; output directories must be empty. The runner prints configuration before calls. Missing configuration/credentials writes REAL_EVAL_BLOCKED artifacts without invoking a fake provider.

Artifacts: `experiment.json` (configuration, execution order and partial progress), `runs/*.json` (per case/arm/repetition), `comparison.json` (arm aggregates, available observation counts and paired per-case differences), `findings.md` (human view) and `traces/*.jsonl` (sanitized trace IDs/counts/timing). Atomic JSON persistence occurs after each run; canceled execution records active partial metrics and cleans temporary directories. Persistence failure stops the experiment. No full transcript, model final text, check script, raw tool output, credentials or unrestricted error text is persisted.

## Measurement interpretation

- PASS/FAIL/ERROR and failed check IDs come from independent final-workspace checks. ERROR contributes zero to task success, with a separate failure category. Real-provider status PASS means all runs pass; FAIL means completed pilot with task failures; BLOCKED means unavailable/failed model requests.
- Tool call counts include attempted calls, including rejected/unknown tools. Completed graph queries are separate. Repeated exploration counts only successful repeated read_file paths. Relevant-file evidence records first successful read or returned graph path matching host ground truth. Bash file discovery/semantic understanding are unavailable; Bash discovery calls use an explicit utility-token heuristic.
- Memory observations are checked candidates, freshness outcomes, unique selected/included/dropped IDs plus event counts, size/budget drops and fixed read-error counts. Retrieval precision is relevant actually included events / all included events. Stale Recall Rate is ineligible inclusion events / observed non-fresh candidate events. These are event metrics, not unique-entry probabilities; null denotes no denominator. Both stale and unknown fixtures must be absent from requests. Relevant ground truth is host-owned.
- Provider usage is actual trace_finish usage when supplied; unknown fields remain null and aggregates disclose available counts. Request token counts/estimate flags are separate and include tool schemas and admitted memory. Constructed requests can exceed successfully delivered provider responses after transport errors. No monetary cost is inferred.
- Elapsed time includes fixture setup, checks and cleanup. Agent timing, graph build/refresh, Memory fixture preparation and retrieval/freshness times are separate. Memory observation adds one bounded independent freshness check for selected entries, included in the measured overhead. Context trim observations retain the existing maximum omission semantics.

Offline fake LLM results validate harness isolation/instrumentation and checks, never model quality. Single repetition real results are descriptive, with no statistical significance or generalization to larger/non-Python projects. Retrieval improvements only establish task-success improvements if deterministic outcomes also improve. Keep #28 open and the branch unmerged until independent review of code and real observations.
