# Native Bash sandbox

The `bash` tool runs every command inside a platform sandbox. On both platforms
the sandbox grants writes to the workspace so ordinary edits work and denies
writes everywhere else. Git metadata inside the workspace needs an explicit rule:
it lives under the writable workspace, and a linked worktree's `.git` file points
at an admin directory that can sit outside it.

Read visibility differs by platform, and the difference is real:

| | Linux (bubblewrap) | macOS (`sandbox-exec`) |
| --- | --- | --- |
| Host paths visible to the command | Only what is explicitly mounted | Everything the invoking user can read |
| Workspace outside the mounts | Not visible | Visible |
| Read restriction mechanism | Mount list (namespace) | None: the profile allows reads by default |

On Linux the sandbox builds a fresh mount namespace, so a path exists inside it
only because the tool mounted it. On macOS the profile starts from
`(allow default)` and narrows writes and (unless granted) network; it does not
narrow reads. A macOS sandboxed command can therefore read host files the user
can read. Treat write isolation as the guarantee there, not read isolation.

## What the sandbox enforces

| Boundary | Linux | macOS |
| --- | --- | --- |
| Workspace source files | Writable (workspace bind) | Writable (profile allow) |
| `.git` directory in a checkout | Read-only | Read-only |
| `.git` file of a linked worktree | Read-only | Read-only |
| Linked worktree admin directory | Read-only, mounted so read-only Git still works | Read-only; already readable |
| Host paths outside the mounts | Not visible | Readable, not writable |
| Network | Denied unless the caller was granted `network_access` | Denied unless granted |
| Credentials | Secret environment keys stripped before the child starts | Same |

Write isolation holds on both platforms. Read isolation is a Linux property of the
mount namespace, and it is exactly why the mount list is treated as a security
surface rather than a convenience.

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
both platforms, so the two rules cannot drift apart.

### Untrusted pointers never widen the mount list

`cwd/.git` is writable by the sandboxed command, so its `gitdir:` text is
untrusted input. On Linux the mount list *is* the read boundary, which makes a
forged pointer a read-escalation attempt: naming `/etc` or a home directory would
otherwise mount it into the namespace.

Two checks therefore gate every external candidate, and each is required because
the other alone is forgeable:

1. `<admin>/gitdir` must resolve back to this workspace's `.git` file. A planted
   directory outside the workspace cannot satisfy it unless it was written by Git
   for this workspace.
2. The admin directory must sit at `<common>/worktrees/<name>`, and `commondir`
   must resolve to exactly that `<common>`. Without the layout check, a directory
   the command creates inside the workspace could present itself as an admin
   directory and steer `commondir` at an ancestor.

A symlinked `.git` or admin directory is refused outright: the mount source would
resolve somewhere the checks never inspected.

### Unsupported layouts fail closed

The workspace bind is writable, so Git metadata stays mutable unless the sandbox
explicitly narrows it. A `.git` entry that this helper cannot pin read-only is
therefore **refused before the command runs**, not skipped. Skipping would leave
the command executing with the whole workspace writable while the real metadata
sat inside it.

Three shapes are supported:

| `cwd/.git` | Behaviour |
| --- | --- |
| absent | No Git metadata; the command runs normally |
| a directory | Pinned read-only; the command runs |
| a file naming a registered `<common>/worktrees/<name>` admin directory | Admin directory and shared Git directory pinned read-only; the command runs |
| a symbolic link | **Refused** |
| a file naming anything else, e.g. `git init --separate-git-dir` | **Refused** |

Refusal raises `UnprotectedGitMetadata` before any sandboxed process starts, so
the command has no effect at all. Both refused shapes can otherwise place live
metadata inside the writable workspace — a symlink to an in-workspace directory,
and a `--separate-git-dir` metadata directory created under the workspace — which
is why they are refused rather than merely left unprotected. Supporting them
would require mounting metadata the helper cannot verify, which would widen host
read access, so they stay unsupported.

## What is not claimed

- A workspace using an unsupported Git layout cannot run sandboxed Bash at all.
  That is deliberate: refuse rather than run with unprotected metadata.

- The workspace is **not** read-only. A command can modify, create and delete
  source files; that is intended.
- Protected metadata is not a backup. A command that corrupts a source file the
  repository tracks still corrupts it.
- The sandbox constrains file and network effects. It does not impose CPU, memory
  or wall-clock quotas beyond the tool's own timeout.
- **Read isolation is a Linux property, not a shared guarantee.** macOS's profile
  allows reads, so a sandboxed command there can read any host file the invoking
  user can read. Read isolation on Linux covers what the mount namespace omits,
  minus the Git admin mounts described above.
- Neither platform should be treated as a general confidentiality boundary for
  host data.
- A host without a working sandbox implementation **fails closed**: the test
  helpers skip rather than report success, and `BashTool` raises on Linux when
  `bwrap` is missing.

## Verifying the boundary

`tests/infra/tools/test_sandbox_git_boundaries.py` drives the real
`BashTool.execute` path and asserts both directions: the metadata writes are
refused, and ordinary source writes plus read-only Git keep working. It also
covers forged `gitdir` and `commondir` pointers and pointer rename/replacement. A
host with no usable sandbox produces SKIP, which is not evidence of enforcement.

```sh
uv run pytest -q tests/infra/tools/test_sandbox_git_boundaries.py -rs
```

## Related

- [Offline coding task E2E](offline-coding-e2e.md) — end-to-end task acceptance
  that runs through this sandbox.
- [GitHub Issue workflows](github-workflow.md) — retained worktrees and the
  repository boundaries the sandbox protects.
