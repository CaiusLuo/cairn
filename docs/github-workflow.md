# Retained coding workflows

These explicit APIs are separate from the interactive CLI. They preserve the
source working tree and use retained Git Worktrees for inspection and recovery.
Issue text, diffs and file contents are untrusted input. A completed coding task
is not evidence of correctness.

## Coding and verification

`GitHubTaskSource` fetches one explicitly selected open Issue through an injectable
`GitHubIssueReader`. The repository, Issue number and base branch are harness
inputs. Closed Issues, PRs and malformed or empty tasks are rejected. Source
metadata stays in `IssueTask`, outside the Agent's core models.

`LocalWorkflow` accepts a WorktreeProvider, a fresh CodingTaskRunner factory and
an explicitly configured `DeterministicVerifier`. It stages tracked and new
untracked changes, rejects empty changes, and binds structured, nonempty checks
to the exact staged Git tree. `FixedChecksVerifier` runs file checks against that
Worktree; a passing EvalSuite fixture is not publication evidence.

Ownership, HEAD, branch, index, file bytes/modes and untracked/ignored inputs are
checked around verification and publication. Missing, malformed, failed or stale
checks block progress. Verifier writes and later edits invalidate verification.
Checks must leave no build or cache artifacts in the Worktree.

## Local review and repair

`FreshReviewer` reviews the original task, exact verified diff and deterministic
checks with a new Agent, AgentState and context on every call. It receives no
Implementer/Fixer history, final response or raw trace. Its registry contains only
`ReadFileTool`, with separate run/context budgets, timeout and trace ID.

Strict JSON must identify the reviewed tree, completeness and unique blocker or
warning findings with safe changed-file locations and behavioral evidence.
Malformed output fails closed. Only a complete validated result without blockers
passes review; that verdict is not proof of correctness. Before/after fingerprints
cover the complete Workspace, including ignored entries, and private Git metadata.
Inspection avoids index writes. These observations cannot detect a transient
change restored between checks.

`ReviewWorkflow(git, reviewer, verifier, max_fix_iterations=..., fixer_llm=...)`
runs an initial review even when the fix limit is zero. Warnings do not trigger
repairs. Blockers invoke a fresh CodingTaskRunner using the original task and
current findings, within the configured limit. Every repair invalidates old
verification, restages changes, reruns deterministic checks and starts a fresh
review. Persistent blockers or no tree progress require human review.

Local review performs no commit, push or PR creation. It is not automatically
connected to GitHubPublisher: after a repair, the original LocalWorkflowResult
is stale. Publication needs fresh evidence for the final unchanged snapshot.

## Publication and credentials

`GitHubPublisher.publish(issue, result)` separately requests NETWORK authorization
through `SessionPermissionHandler`. Denial makes no commit or transport call.
The Agent cannot authorize publication; the normal CLI never publishes implicitly.
The destination repository and base revision must match, and the remote head must
match the verified local commit before a Draft PR is created. `Closes #N` is added
only when `intended_fix` is explicitly enabled.

Authentication stays inside the transport's credential callback and bounded
fixed-host HTTPS. Tokens never enter Git arguments, remote URLs or Agent tools.
Publication creates a unique branch once, without force-push, redirects or
mutation retries. PR bodies contain source, paths, check verdicts and revision
or trace references, without model responses or tool output.

Declare custom credential variable names through `secret_env_keys`. WorktreeHandle
preserves the creation-time exclusions, including every source TOML Provider.
ReviewWorkflow combines them with its own declarations and GitHub credential
names for Fixer Bash and repository-inspection children. Parent environments stay
unchanged. This protects environment-variable transmission; accessible credential
files remain readable. It does not provide a complete process sandbox or change
the existing Agent sandbox and permission policy.

## Recovery and limits

Worktrees remain retained on success and failure. Results keep completed task and
review evidence; atomic private JSON reports beside the Worktree keep safe status,
revision/check summaries, trace references and known remote state. Reports omit
prompts, finding prose, model responses, raw errors, credentials and tool output.
They are diagnostics, not reusable authorization or verification tickets.

Cancellation settles owned work before external CancelledError propagates; notes
identify the retained Worktree and report. Interrupted creation rolls back owned
resources. Persistence failures remain observable. A publication timeout may leave
a remote branch or PR: inspect the destination before another attempt.
The private `.publish-attempt` marker prevents concurrent publication and result
reuse; obtain fresh verification for another attempt. There is no automatic retry
or remote cleanup. Release a live handle explicitly when finished; dirty cleanup
requires `discard_changes=True`, and advanced branches are preserved.

Review accepts complete UTF-8 patches up to 32 KiB, 10,000 filesystem entries and
responses up to 64 KiB/50 findings. Uninspectable diffs cannot pass. Publication
supports SHA-1 repositories and unsigned single-parent commits, with 64 KiB Git
output/object limits, 1024 new objects and 8 MiB total. External filters/Git LFS,
submodules, sparse/hidden index entries, non-UTF-8 paths, percent-containing branch
names and EOL conversions are unsupported. Limit violations preserve recovery
evidence. There is no queue, rebase, merge or automatic publication after review.

## Manual opt-in smoke test

CI uses fake transports/models and local Git. For a real test, use a disposable
repository you control with `main` and `answer.txt` containing `wrong`. Open an
Issue asking for `correct`, clone it, and make the local base match remote `main`.
Configure its TOML/model credentials privately; store the expected `correct` plus
newline outside the clone. Supply a repository-scoped `GITHUB_TOKEN` with Contents
and Pull requests write permissions through your private credential mechanism.

From the Cairn checkout, replace the placeholders and run:

```bash
uv run python examples/github/run_issue_task.py \
  --disposable-repository --owner OWNER --repo REPO --issue NUMBER \
  --source /absolute/path/to/disposable-clone \
  --worktree-parent /absolute/path/outside-the-clone/cairn-tasks \
  --base-ref origin/main --base-branch main \
  --check-file answer.txt --expected-file /absolute/path/to/expected.txt \
  --publish --author-name 'Your Name' --author-email 'you@example.com'
```

Model access and publication require explicit approvals. Omit `--publish` for local
verification only, and `--intended-fix` unless the PR should close the Issue.
Compare the retained Worktree/report with the Draft PR tree, confirm the source
files/index are unchanged, then clean up the disposable repository manually.
