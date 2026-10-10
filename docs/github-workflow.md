# Explicit GitHub Issue workflow

The workflow API is separate from the interactive CLI. It reads one explicitly
selected open Issue, creates a unique retained local Worktree, and calls the
existing CodingTaskRunner once. The source working tree and index are preserved.
Issue title/body are untrusted task input, never permission or publication policy.

`GitHubTaskSource` uses an injectable `GitHubIssueReader`. The source repository,
Issue number and base branch are explicit harness inputs. Closed Issues, PRs,
missing or malformed Issues and empty instructions are rejected. GitHub source
metadata stays in `IssueTask`, outside the Agent's TaskSpec and core models.

`LocalWorkflow` accepts a WorktreeProvider, a factory for a fresh CodingTaskRunner
in that Worktree, and an explicitly chosen `DeterministicVerifier`. A normal task
completion is not verification. `FixedChecksVerifier` runs existing deterministic
file checks directly against the task Worktree, without EvalRunner's temporary
fixtures. An injected verifier can run fixed local test commands; its validated
`VerificationResult` must contain nonempty, uniquely identified checks for the
supplied staged tree. The Issue and Agent cannot select the verifier or produce
its result.

The harness stages tracked and new untracked files and captures `git write-tree`.
Verification is bound to that immutable tree. Ownership, HEAD, branch, index,
untracked/ignored files, file modes, symlinks and raw file bytes are checked before
and after verification, after authorization, before commit/publication and before
Draft PR creation. Verifier writes or later edits invalidate the result. No
changes, failed tasks, absent/malformed/failed/stale checks and drift block remote
publication. A previously passing EvalSuite fixture is not accepted as evidence.

`GitHubPublisher.publish(issue, result)` is a separate explicit harness call.
It requests NETWORK through `SessionPermissionHandler.authorize`, using the
existing allow-once/session/deny choices. A denied request performs no transport
calls or commit. The operation is not registered as an Agent tool. The normal
`cairn` command never publishes implicitly.

The real adapter uses bounded fixed-host HTTPS to GitHub's Git Database API.
It uploads the exact unsigned local commit's new objects, checks returned object
and commit SHA-1 values, and creates the unique branch reference once. It never
updates an existing remote branch, force-pushes, follows redirects or retries a
mutation. The explicit repository and base revision must match before publication;
the head must match the committed revision before the Draft PR is created.
Only Draft PRs are supported. PR text contains source URL, changed paths, check
identifiers/verdicts, tree/commit and trace ID, without model answers or tool
output. `Closes #N` is added only with the explicit `intended_fix` option.

Authentication happens only inside the HTTP transport through its supplied
credential callback. No token is placed in Git argv, remotes or subprocess env.
LocalWorkflow strips GH_TOKEN, GITHUB_TOKEN, CAIRN_GITHUB_TOKEN, all source TOML
provider credential variables and explicitly supplied `secret_env_keys` from
Agent child processes. Declare every custom GitHub credential variable through
that option. CodingTaskRunner also isolates the Git children used to refresh Agent
repository context; the interactive CLI keeps its existing context policy.
This protects environment-variable transmission; it does not prevent
reading accessible local credential files and is not a complete process sandbox.
The existing Agent sandbox/permission policy is unchanged.

## Recovery

Every successfully created Worktree is retained, including after successful
publication. An atomic mode-0600 JSON report sits beside it in the caller's parent
directory. Reports record phase, status/failure category, source/destination,
branch, Worktree path, base/head/tree, checks, trace ID and available remote state.
They contain no credential, conversation, provider exception or command output.

Cancellation settles owned work before propagating the original CancelledError.
The exception notes include the retained path, branch and report location. If
creation itself is interrupted before returning a handle, WorktreeProvider rolls
back its partial creation. Report-write failures are observable typed errors;
the retained path remains available in exception notes.

After a publication attempt, `remote_published: null` means the outcome is unknown;
`true` means branch publication was confirmed. A PR failure preserves the local
commit, verified tree and remote branch information. A timeout/cancellation may
leave a remote branch or PR even when its response was not received. Inspect the
explicit destination and head manually before attempting further remote actions.
There are no automatic retries, branch deletions or PR closures.

Recovery reports are diagnostics, not reusable authorization/verification tickets.
An exclusive private `.publish-attempt` marker beside the report prevents concurrent
publication and reuse of the original result. It remains after every outcome;
rejected attempts preserve the previous report, including known remote outcomes.
For a new publication attempt, rerun deterministic verification and obtain fresh
in-memory evidence. During the owning process, explicitly call
`await result.handle.release()` when finished. Dirty files require an explicit
`discard_changes=True`; advanced branches are preserved. After process exit,
inspect with normal Git commands and remove the Worktree manually only when its
contents are no longer needed.

## Manual opt-in disposable smoke test

CI uses fake HTTP, fake LLMs and local Git only. The following manual example
uses a real model and GitHub; it must be run only against a disposable repository.

1. Create a disposable GitHub repository you control with a `main` branch and an
   `answer.txt` file containing `wrong`. Open an Issue asking to replace its content
   with `correct` and no other changes. Clone it locally. Do not use a production
   repository. The local base must exactly match the remote `main` revision.
2. Configure that clone's `.cairn/models.toml` and its model credential privately,
   or use its `.env` when TOML is absent. The example uses the first configured
   provider/model group and its existing runtime/fallback boundary. Put the
   expected content `correct` plus newline in a file outside the clone.
3. Provide a repository-scoped GitHub token in GITHUB_TOKEN using your usual
   private credential mechanism. For a fine-grained token, allow Contents write
   and Pull requests write on this disposable repository only. Do not place the
   token in a command argument, URL or tracked file.
4. From the Cairn checkout, run the explicit example (replace all placeholders):

   ```bash
   uv run python examples/github/run_issue_task.py \
     --disposable-repository --owner OWNER --repo REPO --issue NUMBER \
     --source /absolute/path/to/disposable-clone \
     --worktree-parent /absolute/path/outside-the-clone/cairn-tasks \
     --base-ref origin/main --base-branch main \
     --check-file answer.txt --expected-file /absolute/path/to/expected.txt \
     --publish --author-name 'Your Name' --author-email 'you@example.com'
   ```

   Read/network, project model access and publication use explicit approvals.
   Omitting `--publish` stops after local verification. Omit `--intended-fix` unless
   the Draft PR is intended to close that Issue. A denied publication leaves local
   edits, the unique branch and recovery report without remote mutations.
5. Check the report and retained Worktree, confirm the source's original files and
   index are unchanged, and inspect the remote Draft PR's head/tree against the
   report. Afterwards clean up this disposable repository and its local Worktree
   yourself. Do not merge it into a real project as part of this smoke test.

## Current bounds

The first implementation supports small SHA-1 repositories and unsigned single-
parent commits. Git commands retain bounded 64 KiB output; new publication objects
are at most 64 KiB each, 1024 objects and 8 MiB total. Limit violations block safely
and preserve recovery evidence. Git LFS/external filters, submodules, sparse or
hidden index entries, non-UTF-8 tree names and percent-containing branch names are
unsupported. Raw verifier bytes must match staged blobs, so EOL conversion is
rejected. Ignored build/cache artifacts also invalidate verification: use checks
that leave the Worktree unchanged. There are no queues, Reviewer Agent, rebase,
merge or automatic remote recovery.
