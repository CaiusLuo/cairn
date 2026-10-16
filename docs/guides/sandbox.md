# Native Bash sandbox

The `bash` tool runs every command inside a platform sandbox. The sandbox grants
writes to the workspace so ordinary edits work, and denies everything else. Git
metadata inside the workspace is the exception that needs an explicit rule: it
lives under the writable workspace, and a linked worktree's `.git` file points at
an admin directory that can sit outside it.

## What the sandbox enforces

| Boundary | Rule |
| --- | --- |
| Workspace source files | Writable: the workspace root is bound read-write |
| `.git` directory in a checkout | Read-only |
| `.git` file of a linked worktree | Read-only |
| Linked worktree admin directory | Read-only, mounted so read-only Git still works |
| Everything else on the host | Not visible unless it is a system path |
| Network | Denied unless the caller was granted `network_access` |
| Credentials | Secret environment keys are stripped before the child starts |

The workspace stays writable because that is the tool's purpose. Everything the
sandbox protects is protected by narrowing that grant, not by trusting the
command.

## Two checkout shapes

A sandboxed command sees one of two Git layouts, and both must be protected.

A **direct checkout** has a `.git` directory inside the workspace:

```text
workspace/
  .git/            <- read-only
  src/             <- writable
```

A **linked worktree** has a `.git` file instead, holding a `gitdir:` pointer to
an admin directory under the main repository. That repository is often outside
the workspace:

```text
workspace/                 main-repo/
  .git  -> gitdir: ...       .git/
  src/                         worktrees/linked/   <- read-only
                               objects/            <- read-only (commondir)
```

Read-only Git commands need the admin directory and the shared main Git
directory (`objects`, `refs`) reachable. Mounting them read-only satisfies that
without granting a write the workspace bind did not already grant. Without them,
`git log` fails inside a worktree.

## How each platform applies it

**Linux (bubblewrap).** The workspace is bound writable, then each Git metadata
path is bound read-only *after* it. Bubblewrap applies mounts in order, so the
later read-only mount shadows the earlier writable one:

```sh
bwrap ... --bind <cwd> <cwd> --chdir <cwd> \
      --ro-bind <cwd>/.git <cwd>/.git
```

**macOS (`sandbox-exec`).** The profile allows writes under the workspace and then
denies writes to each Git metadata path. `sandbox-exec` evaluates rules in order,
so the later deny narrows the earlier allow:

```text
(deny file-write*)
(allow file-write* (literal "/dev/null") (subpath "<cwd>"))
(deny file-write* (subpath "<cwd>/.git"))
```

`_git_metadata_paths()` in `src/cairn/infra/tools/bash.py` resolves the paths for
both platforms, so the two rules cannot drift apart. Paths that do not exist are
skipped: naming an absent path never grants access.

## What is not claimed

- The workspace is **not** read-only. A command can modify, create and delete
  source files; that is intended.
- Protected metadata is not a backup. A command that corrupts a source file the
  repository tracks still corrupts it.
- The sandbox constrains file and network effects. It does not impose CPU, memory
  or wall-clock quotas beyond the tool's own timeout.
- Files outside the workspace and the system paths are not readable, but an
  operator should not treat the sandbox as a general confidentiality boundary for
  host data.
- A host without a working sandbox implementation **fails closed**: the test
  helpers skip rather than report success, and `BashTool` raises on Linux when
  `bwrap` is missing.

## Verifying the boundary

`tests/infra/tools/test_sandbox_git_boundaries.py` drives the real
`BashTool.execute` path and asserts both directions: the metadata writes are
refused, and ordinary source writes plus read-only Git keep working. A host with
no usable sandbox produces SKIP, which is not evidence of enforcement.

```sh
uv run pytest -q tests/infra/tools/test_sandbox_git_boundaries.py -rs
```

## Related

- [Offline coding task E2E](offline-coding-e2e.md) — end-to-end task acceptance
  that runs through this sandbox.
- [GitHub Issue workflows](github-workflow.md) — retained worktrees and the
  repository boundaries the sandbox protects.
