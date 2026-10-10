# Bounded local review

`FreshReviewer` reviews one staged, deterministically verified proposal in an
owned Worktree. Every call constructs a new Agent, AgentState and context builder.
It uses the existing `run_turn()` with a caller-selected RunBudget, context budget
and timeout. Its dedicated registry contains only `ReadFileTool`; there is no
shell, edit or network tool and no Implementer/Fixer history in its context.

The harness supplies the original TaskSpec, the exact HEAD-to-staged-tree patch,
branch/tree facts and structured deterministic check verdicts. Task text, diff
and file contents are untrusted data. Model output must be strict JSON with the
matching `reviewed_tree`, a `complete` boolean and a `findings` array. Every finding
has a unique ID, blocker/warning severity, safe changed-file path, positive line,
summary and behavioral evidence. Missing fields, extra fields, duplicate keys or
findings and mismatched trees fail closed. Only a completed, validated empty list
means no reported findings. This is a review verdict, not proof of correctness.

Before and after review, the harness checks Worktree ownership, HEAD, branch,
index, tracked bytes/modes, untracked and ignored inputs. A separate fingerprint
checks all Workspace entries and private Git metadata, including empty directories
and file identity/modes/times. Git inspection uses optional locks disabled and an
index-to-tree comparison, avoiding `write-tree` and its index writes. Any detected
drift invalidates the verdict. As with before/after checks generally, this cannot
prove that a transient change was never made and restored between observations.

## Review and repair

`ReviewWorkflow(git, reviewer, verifier, max_fix_iterations=..., fixer_llm=...)`
accepts an owned WorkflowGit, an initial GitSnapshot and valid passing
VerificationResult through `run(task, snapshot, verification)`. It retains the
Worktree and always performs an initial review, including when the fix limit is
zero. Warnings do not request fixes. No blockers finishes with `review_passed`;
blockers at the limit finish with `needs_human_review`.

Each permitted repair constructs a fresh CodingTaskRunner with the original task
and current blockers. Its history is not shared with any Reviewer, and its normal
sandbox/permission boundary remains intact. The old verification is invalidated
before repair starts. A completed task is restaged, verified against the new tree
by the harness-selected DeterministicVerifier, checked for drift and reviewed
again with fresh context. Missing, malformed, stale or failing checks block the
next review. A repair that makes no tree progress stops for human review. Reviewer
and Fixer budgets, timeouts and trace IDs are separate.

Runtime errors, malformed output, budget exhaustion, timeout, drift and
cancellation produce explicit status/failure categories. Cooperative cancellation
returns a cancelled result. External asyncio cancellation propagates the original
CancelledError after owned reviewer/fixer/verifier work settles, including repeated
cancellation. No background worker survives the call.

## Evidence and recovery

The in-memory result retains completed review rounds, their exact snapshots and
checks, and full Fixer TaskResults. An atomic private JSON report beside the
Worktree records phase, status/failure, owned path/branch, current tree/checks,
completed reviewer traces and safe finding locations, plus Fixer traces and task
repository summaries. It does not persist prompts, diffs, finding prose, task
responses, raw provider errors or tool output. Failed re-verification checks remain
observable; invalidated verification is absent. Report-write failures propagate
as ReviewPersistenceError with the retained path in exception notes. Reports are
diagnostics, not reusable authorization or verification tickets.

This API is separate from the interactive CLI and GitHubPublisher. It performs
no commit, push, PR creation or merge. There is no automatic Issue #20 adapter:
after a Fixer, the original LocalWorkflowResult is stale and must not be reused.
Any later publication integration must explicitly obtain fresh publication
evidence and use the final unchanged snapshot/checks.

## Current bounds

Review supports complete UTF-8 text patches up to 32 KiB and Workspace inspection
up to 10,000 filesystem entries. Binary, oversized or otherwise uninspectable
diffs return incomplete; no truncated patch is approved. Response JSON is limited
to 64 KiB and 50 findings. Existing WorkflowGit restrictions remain, including
unsupported external filters, submodules and hidden index entries, and rejection
of untracked/ignored artifacts during verification. Checks should leave no build
or cache artifacts in the Worktree. Local read-only tooling does not constitute a
complete process sandbox for the injected model client or harness callbacks.
