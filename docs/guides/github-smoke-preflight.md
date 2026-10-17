# GitHub Draft PR smoke preflight

Offline preparation for the real, one-shot GitHub publication gate
([Issue #50](https://github.com/CaiusLuo/cairn/issues/50)). This document records
what the current code actually does, what the Owner must authorize, and how the
remote result must be independently verified. It contains **no** real run.

The workflow this gate exercises is described in
[GitHub Issue workflows](github-workflow.md). This page is the execution contract
for one authorized run, not a replacement for that guide.

## Current status

```text
READY_FOR_AUTH
```

Nothing in the offline audit blocks the run. Phase 2 is gated only on the Owner
supplying the authorization list below. No real Issue read, Worktree, provider
call, push or Draft PR has been performed, and none is claimed.

## Scope and non-goals

- One Issue, one retained Worktree, one coding task, one deterministic check, one
  branch, one Draft PR, in a **disposable private repository**.
- Exactly one paid provider run, under a stated cost ceiling.
- Not a concurrency, retry, merge, rebase or force-push test. There is no automatic
  retry and no remote cleanup.
- Not evidence that the Agent writes good code. A passing deterministic check and a
  Draft PR are not correctness proof.

## Audited behaviour

### Entry point and permissions

`scripts/github/run_issue_task.py` is the only publication entry point. The public
CLI (`cairn`) exposes the `session` command group; nothing in the CLI publishes
implicitly.

The script requires the explicit `--disposable-repository` opt-in before any
network access, and validates `--token-env` as a bare environment-variable name so
a credential cannot be passed inline as an argument.

Two independent approvals are required:

1. **Issue read** — one `SessionPermissionHandler.authorize` call for
   `NETWORK`, justified with the repository and Issue number, before the first
   REST request. Denial prints `Issue read denied.` and exits 1 without network I/O.
2. **Publication** — `GitHubPublisher.publish` performs its own separate `NETWORK`
   authorization naming the repository, base, head and verified tree. A denial
   makes no commit and no transport call. The Agent cannot authorize publication.

Both approvals use the same `SessionPermissionHandler` instance, so choosing
"allow for this session" at the Issue read also covers the later publication
prompt. The run must be observed by a human who reads each prompt; a session grant
is not a substitute for the authorization list below.

Model access is approved separately through `confirm_provider_access`, which
fails closed without an interactive TTY, and is required for every non-`env`
layout. The script matches the CLI order: select the provider, validate its
configuration, approve it, and only then resolve its credential.

### Provider selection

Provider selection reuses the CLI's own logic rather than a test-only path:

- `load_project_providers(source/.cairn/models.toml)` detects the layout.
- `default_provider(...)` picks the first declared provider, or the `env`-layout
  implicit provider from `CAIRN_*` values.
- The first model group (`model_config[0]`) is the one used for this run.
- `resolve_provider_api_key(...)` resolves only that provider's credential, after
  approval.
- `build_provider_runtime(...)` is constructed before any coding task runs, and the
  provider/model bindings are committed only after its admission is rebuilt.

Fallback candidates are not exercised by this smoke; it is a single-provider run.

### Required GitHub token scopes

Derived from the REST calls in `src/cairn/infra/github/http.py` and
`src/cairn/infra/github/transport.py`; nothing is pushed over Git, so no Git
credential or `credential.helper` is involved anywhere.

Publication uploads objects and creates a branch and a Draft PR. On a
**fine-grained personal access token** scoped to the one disposable repository:

| Permission | Level | Why |
| --- | --- | --- |
| Metadata | Read | Implied mandatory baseline for any fine-grained token |
| Contents | Read and write | `POST /git/blobs`, `/git/trees`, `/git/commits`, `/git/refs` create the branch |
| Pull requests | Read and write | `POST /pulls` creates the Draft PR |
| Issues | Read | `GET /repos/{owner}/{repo}/issues/{number}` reads the source Issue |

If a **classic** token is used instead, it needs the `repo` scope, which is broader
than this run requires. Prefer the fine-grained token; it is the minimal grant.

Additional required properties of the credential:

- It must authorize **only** the disposable repository, not the Cairn repository.
- It must not carry `workflow`, `administration`, `secrets`, or organization-wide
  scopes.
- It must be supplied as the environment variable named by `--token-env`. The
  script reads it with `os.environ.get` at request time and never logs it. Note
  that the default `--token-env` is `GITHUB_TOKEN`, which is also a name that Bash
  tools would otherwise inherit; see the credential handling section.
- It must never be written into the disposable clone's `.git/config`, remote URL,
  `.env`, or any tracked file.

A token with insufficient scope fails at the first API call and produces a remote
`BLOCKED` outcome. It is never escalated by reading a higher-privilege credential
from `gh`. Obtaining a token via `gh auth token` is explicitly out of bounds.

### Publish path

Publication is REST-only and creates objects in a fixed order, verifying each one:

1. `verify_destination` — repository identity, base branch at the verified base
   revision, and absence of the head branch (`404` is the only accepted "free").
2. `commit-tree` builds the commit locally; the tree, parent, author/committer and
   whole-second UTC date are hashed and must match the commit SHA.
3. Each new blob and tree is uploaded; the returned SHA must equal the local object
   SHA, which prevents the remote from silently rewriting bytes.
4. `POST /git/commits` must return the locally computed commit SHA.
5. `POST /git/refs` must return the requested ref and that same SHA, over the
   expected fixed-host URL.
6. `POST /pulls` must return `draft: true`, the head SHA/ref, the base ref/SHA, both
   repositories matching the destination, and an `https://github.com/...` URL
   naming exactly this repository and PR number.

Guards that hold before any mutation: the verification must belong to the same
source URL, Issue number, repository, base branch and `intended_fix` flag; the
snapshot branch and base revision must match the handle; the verification must be
current (its tree equals the snapshot tree); the change set must be non-empty; and
the Worktree must be unchanged at every step via `assert_unchanged`.

There is no force-push, no redirect following, no proxy, no mutation retry, and no
automatic remote branch or PR deletion. A private `.publish-attempt` marker makes a
second publication attempt on the same result impossible; another attempt needs
fresh local verification. `Closes #N` is added only with `--intended-fix`.

`--base-ref` only resolves a local commit; the local-to-remote base binding is
enforced later by `verify_destination`, which requires the remote base branch to be
at exactly the verified base revision. A stale local clone therefore fails closed
rather than publishing onto a moved `main`.

### Credential handling in child processes

Two distinct boundaries matter, and they must not be confused:

- **Git children** use an allowlist (`build_git_env`). Only a fixed set of names
  survives, minus the declared credentials. The declared names come from
  `SECRET_ENV_KEYS`, the source TOML providers, the example's `--token-env`, and
  `GITHUB_SECRET_ENV_KEYS`. `LocalWorkflow` tightens the provider's policy before
  it creates the Worktree, so the token is excluded from every Git child.
- **Agent Bash** deliberately inherits the full host environment, then strips the
  declared credential names. A token exported into the launching shell is therefore
  contained only because the runtime declares that name to the tool policy;
  without the declaration it would be visible to Agent commands.

Consequently a Phase 2 run should supply the token through a mechanism that does
not leave it exported in the launching shell, and must confirm the name is
declared. The explicit declaration path (the `--token-env` value) is what makes
this hold; see the test referenced below.

### Worktree lifecycle and ownership

| Fact | Owner |
| --- | --- |
| Creating the Worktree and branch | `WorktreeProvider.create`, called by `LocalWorkflow._execute` |
| Retention | `LocalWorkflow` calls `handle.retain()` immediately after creation |
| Branch naming | Harness: `codex/issue-<N>-<uuid4>` |
| Parent directory | Caller (`--worktree-parent`); must be outside the source tree |
| Cleanup | **Nobody automatically.** Cleanup is manual and explicit |

Worktrees are retained on success **and** on failure. The recovery report is the
atomic private JSON written beside the Worktree at
`<worktree-parent>/<worktree-name>.json`. Release is a deliberate human action:
`handle.release()` preserves dirty files by default, needs
`discard_changes=True` to drop them, and preserves advanced branches. The
`.publish-attempt` marker prevents concurrent or repeated publication on the same
result. Cancellation settles owned work before propagating and annotates the
retained Worktree and report.

The evidence actually retained after a run is the source clone, the Worktree, the
branch, the recovery report, and the JSONL trace at `<worktree-parent>/traces`.
Issue text, diffs and file contents are untrusted input throughout.

## What the Owner must authorize

Phase 2 cannot start until the Owner supplies every item below. Any missing item
means the run does not start; it does not start partially.

**A. Exact disposable repository**

1. Owner/name of a **private** GitHub repository created solely for this gate.
   The Cairn main repository must not be used.
2. Confirmation that the repository contains `main` with a tracked `answer.txt`
   whose exact content is `wrong` plus a newline.
3. One open Issue in that repository requesting that `answer.txt` become `correct`
   plus a newline, with the Issue number stated.
4. Confirmation that the repository has no branch, tag or PR that would collide
   with a `codex/issue-<N>-*` branch, and that branch protection will not reject
   the new branch or Draft PR.

**B. Local base binding**

5. A clone path for the disposable repository whose local `main` is at the same
   commit as the remote `main` at run time, plus the SHA the Owner expects.
6. An absolute `--worktree-parent` path outside that clone.

**C. Credential**

7. The fine-grained token's permission set, listed explicitly, and the
   environment-variable name to use via `--token-env`. Confirm `Contents: write`,
   `Pull requests: write`, `Issues: read`, `Metadata: read`, repository scope only.
8. Confirmation that this token is not the one used to operate the Cairn
   repository and carries no organization-wide grant.
9. The token delivery method (interactive prompt, short-lived shell export, or a
   credential store), and confirmation it will not be committed or copied into the
   clone.

**D. Provider and cost**

10. The provider and model group to use, and confirmation that project model
    configuration will be present privately at the clone's
    `.cairn/models.toml` (or the `env` layout will be used).
11. **An independently enforceable spend limit.** `RunBudget(max_steps=20)` is a
    *step* limit fixed in the script; it is **not** a money ceiling, and this repo
    has no runtime USD cap. Stating a dollar figure here does not enforce it, so
    Phase 2 is conditional on one of:
    - a provider-account or API-key-side quota/limit set before the run, or
    - a separate runtime enforcement wired into the harness for this run.

    Record which mechanism is in force and its numeric limit. `max_steps=20` bounds
    the number of model calls and therefore the *exposure*, but it must not be
    described as a spend cap.
12. A ceiling on any retry. Authorize exactly one attempt: there is no automatic
    retry, and a `.publish-attempt` marker blocks reuse of the result.

**E. Human presence and containment of authority**

13. A human available for the whole run to answer the Issue-read and publication
    permission prompts and the provider approval. Declining any prompt is a valid
    `BLOCKED` outcome and must be recorded as such.
14. Explicit authorization to create the remote branch and the Draft PR, and
    explicit non-authorization of merge, force-push, deletion, and of closing or
    commenting on the Issue.

**F. Evidence retention**

15. Confirmation that the source clone, Worktree, recovery report, trace, and the
    Draft PR are retained as evidence and not deleted, and that the disposable
    repository is deleted only after the evidence has been independently reviewed.
16. Where the evidence package is to be stored, and who reviews it.

## Execution procedure (Phase 2, authorized only)

Run from the Cairn checkout, with every placeholder replaced by authorized values:

```bash
uv run python scripts/github/run_issue_task.py \
  --disposable-repository --owner OWNER --repo REPO --issue NUMBER \
  --source /absolute/path/to/disposable-clone \
  --worktree-parent /absolute/path/outside-the-clone/cairn-tasks \
  --base-ref origin/main --base-branch main \
  --check-file answer.txt --expected-file /absolute/path/to/expected.txt \
  --token-env DISPOSABLE_GH_TOKEN \
  --publish --author-name 'Your Name' --author-email 'you@example.invalid'
```

Do not add `--intended-fix` for this gate. The reason is scope, not evidence
destruction: GitHub closing keywords do **not** close anything while the PR is an
unmerged Draft, and this gate never merges. Omitting them simply avoids creating an
unintended Issue–PR linkage that a later merge would activate; closing the Issue
remains the Owner's explicit decision.

Capture before the run: the source clone's `git rev-parse HEAD`, `git status
--porcelain`, `git rev-parse main`, and the SHA-1 of `.git/index`. Capture after the
run and compare; they must be identical.

The command prints the local status, the Worktree path, the recovery path, the
publication status, and the PR URL. Record them verbatim.

## Remote Draft PR verification scheme

The point of the gate is that a remote fact matches a local fact. Do not trust the
run's own printed status; verify each of these independently, and record the
command and its output for each.

### 1. Local verified facts (before publication)

From the recovery report and the retained Worktree:

- `tree_revision` — the staged tree the deterministic checks ran against.
- `head_revision` after publication — the commit SHA the publisher pushed.
- `changed_files`, and the check verdict list.
- `answer.txt` bytes in the Worktree.

These are the reference values. The recovery report is diagnostics, not a reusable
authorization ticket.

### 2. Remote head identity

```bash
gh api repos/OWNER/REPO/git/ref/heads/codex/issue-N-HEX --jq .object.sha
```

Must equal the local `head_revision` exactly.

### 3. Remote commit and tree

```bash
gh api repos/OWNER/REPO/git/commits/REMOTE_HEAD --jq '.sha, .tree.sha, .parents[0].sha'
```

- `.sha` equals the local `head_revision`.
- `.tree.sha` equals the local verified `tree_revision`. **This is the load-bearing
  check**: it proves the tree the checks passed is the tree that exists remotely,
  independent of commit metadata.
- `.parents[0].sha` equals the authorized base SHA, confirming a single-parent
  commit on the intended base and therefore no unexpected rewriting.

### 4. Remote file bytes

Fetch the blob SHA from the remote tree, then the blob itself, and reconstruct the
raw bytes rather than trusting a rendered view:

```bash
# Repository-scoped evidence token, not the ambient high-privilege gh session.
export GH_TOKEN="$DISPOSABLE_GH_TOKEN"

gh api repos/OWNER/REPO/git/trees/REMOTE_TREE \
  --jq '.tree[] | select(.path=="answer.txt") | .sha'

# Portable decode: `base64 -d` is GNU-only and macOS wants -D, so decode in Python.
gh api repos/OWNER/REPO/git/blobs/BLOB_SHA --jq .content \
  | python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' \
  | sha256sum 2>/dev/null || \
  gh api repos/OWNER/REPO/git/blobs/BLOB_SHA --jq .content \
  | python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' \
  | shasum -a 256
```

All evidence reads use the **same repository-scoped token** the run used
(`GH_TOKEN="$DISPOSABLE_GH_TOKEN"`), never `gh auth token` or the operator's ambient
credential, so verification cannot quietly exercise broader authority than the run
was authorized for. `sha256sum` exists on Linux; `shasum -a 256` on macOS.

The local file's SHA-256 must match. Compare actual bytes, not a summary. Confirm
the decoded content is exactly `correct` plus a newline, and that the mode is
`100644`.

To detect a remote rewrite that preserved the tree but changed other objects, also
compare `git cat-file -p REMOTE_TREE` locally against the remote tree listing for
the changed path.

### 5. Draft PR identity

```bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER \
  --jq '.draft, .state, .head.sha, .head.ref, .base.ref, .base.sha, .head.repo.full_name, .base.repo.full_name'
```

- `.draft` is `true`. A non-draft PR fails the gate: the transport only ever
  requests a Draft, so a non-draft result means something else created or promoted
  it.
- `.state` is `open`; it must not be merged or closed.
- `.head.sha` equals the local `head_revision`; `.base.sha` equals the base SHA.
- Both repositories are the disposable repository.

Then confirm the PR URL is `https://github.com/OWNER/REPO/pull/PR_NUMBER` and read
the body: it must name the Issue source URL, the changed paths, the check verdicts,
the verified tree, the commit, and the trace ID. It must contain no model response
and no tool output.

### 6. Issue linkage

Without `--intended-fix`, the PR body must **not** contain `Closes #N`, and the
Issue must still be open. Confirm the Issue was not closed, labeled, or commented
on by the run.

### 7. Source clone unchanged

Re-compare `HEAD`, `main`, `status --porcelain`, and the index SHA against the
pre-run capture. The clone must be byte-identical in those respects. Confirm
`git worktree list` shows the retained Worktree as registered, and that the
Worktree directory and its recovery report still exist.

### 8. Registry and remote state clean

Confirm exactly one new remote branch and one Draft PR exist, and that no tag, no
additional branch, and no force-push occurred. `gh api repos/OWNER/REPO/branches`
and the commit's parents are sufficient for this.

### Verdict

The gate passes only when sections 2 through 7 all match **and** section 8 shows no
extra remote mutation, with each command's raw output retained. Any mismatch, any
denied prompt, and any scope failure is `BLOCKED` with the raw output retained.
Nothing in this section may be satisfied by the run's own summary.

## Offline coverage

`tests/examples/test_github_issue_task.py` exercises the example's argument
parsing and the credential-declaration contract without network access. It runs
the script with the Worktree creation stubbed to capture the provider's declared
credentials, and separately asserts that a declared token is stripped from Git and
Bash child environments.

**Scope of these tests, stated precisely.** They establish the *declared-credential
boundary* — that a declared secret does not reach Git or Bash children — and the
argument contract. They do **not** exercise the live external permission path: no
real Issue read, no real publication approval, no REST call and no Draft PR is
reached, because the transport is never invoked. Neither do they prove that the
publish path's remote verification works. Treat this as evidence that the
credential surface is shaped correctly, not as proof of complete permission
gating; only the authorized Phase 2 run can establish the latter.

Run it with:

```bash
uv run pytest tests/examples tests/infra/git
```

## Known limitations

- No real provider call, GitHub request, push or PR was performed during this
  preparation. Every statement above about remote behaviour is a code reading, not
  an observation. The remote verification scheme is unexecuted.
- The smoke exercises a single provider with a single model group. Fallback,
  additional providers, and multi-model routing are not covered.
- Publication supports SHA-1 repositories, unsigned single-parent commits, and the
  object/size limits in [GitHub Issue workflows](github-workflow.md). Git LFS,
  submodules, external filters and non-UTF-8 paths are rejected rather than tested.
- A publication timeout can leave a remote branch or Draft PR, and nothing cleans
  it up automatically. Reconcile the remote state before any further attempt.
- Bundling is out of scope: this gate covers one Issue and one deterministic check,
  not review-driven repair (`ReviewWorkflow`), whose result is not wired to
  `GitHubPublisher`.
