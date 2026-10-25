# Local command execution

Cairn is **local-first**. The `bash` tool runs a command as the invoking user, in
the Workspace directory, with the invoking user's environment. There is no OS
sandbox on any platform, and this document makes no isolation claim: a command
can read and write whatever the user can, including files outside the Workspace
and the network.

The operator, not a mount namespace, decides whether Cairn may run a shell.

## What the tool still guarantees

| Guarantee | How |
| --- | --- |
| Starts in the Workspace | The child is spawned with `cwd=workspace.root` |
| Host-like environment | The full host environment is inherited |
| Cairn credentials never leak | `SECRET_ENV_KEYS` plus every configured provider key are stripped |
| Bounded output | stdout/stderr are capped at 64 KiB each, with truncation reported |
| Bounded runtime | A fixed timeout; a timed-out command is killed and reported |
| No leaked children | The process group is killed and reaped before `execute` returns |
| Failures stay observable | Exit code, stderr and truncation flags are always returned |

Cancellation is handled the same way: the group is signalled, settlement is
bounded, and a cleanup failure never replaces the primary outcome.

## What it does not guarantee

- **No filesystem boundary.** A command can read or write any path the user can,
  including `~/.ssh` and `.git` metadata. The Workspace is a starting directory,
  not a fence.
- **No network boundary.** `network_access` no longer exists on the shell: it
  could not have been enforced, so claiming it would have been false. A command
  that reaches the network simply succeeds.
- **No `.git` write protection.** Git metadata is only protected for the *file
  tools*, which still refuse paths inside `.git`.

The file tools keep their own, independent boundary. A path inside the Workspace
obeys `infra.workspace.paths`: no `..`, no `.git` component, no symlinked
component. A path outside it is handled by the trust and grant rules below.

`glob` and `grep` both accept a `path` argument, so they **can** search outside
the Workspace. Their rule follows the *candidate*, not the root — a search rooted
inside the Workspace re-checks every match against `infra.workspace.paths`, so
naming a subdirectory is not a way around it. Git metadata (`.git`) is never
returned from any root, and credential files are never returned from any root
either.

## Workspace trust and outside writes

A local Cairn edits the directory it was started in, so that directory is trusted
explicitly before the first turn:

```text
Cairn will read and write files in this directory:
  /path/to/project

Trust this directory for this session? [y/N]
```

Denial — or no interactive terminal — stops before any turn runs. The grant lives
in the process and is never written to disk.

Once trusted, reads and writes inside the directory need no further approval.
A path **outside** it is handled differently:

| Access | Outside the Workspace |
| --- | --- |
| Read (`read_file`, `grep` with an explicit `path`) | Allowed, no approval, except credential files |
| Write (`write_file`, `edit_file`, `patch_file`) | Needs a grant on the containing directory |
| `glob` with `path` | Allowed, no approval; `.git` and credential files are still withheld |
| Any read of a credential file | Refused with `permission_required` |

An approved directory covers its whole subtree for the rest of the session, so
approving `/tmp/data` once covers `/tmp/data/a.txt` and `/tmp/data/b.txt`. `.git`
stays unreachable wherever it appears.

### Credential files

A credential file is not ordinary data, so it is the one exception to "reading
needs no approval". `infra.tools.sensitive_paths` decides which paths hold
credential material, and every tool that would return its *content* refuses with
a `permission_required` outcome naming the target: `read_file`, `grep`,
`edit_file` and `patch_file`. `write_file` refuses the same targets when its
`expected_content` guard would otherwise read them to compare.

The rule applies inside the Workspace as well as outside it: a tool boundary, not
a location. `glob` reports the names it withheld instead of returning them, so the
model learns the material exists and can ask for access rather than seeing
nothing.

Preflight checks only that the path is a well-formed target; the grant is enforced
when the tool executes, so a refusal is a tool error the model can act on rather
than an exception during approval.

## Approval

A local shell is a host-level capability, so it is asked for explicitly. The first
`bash` call in a session raises a permission request; approving "for this session"
trusts every later command until the session ends, and the grant is never written
to disk. Denial — or no interactive approval path — means nothing runs.

During an interactive turn, runtime tool execution awaits the terminal's approval
surface cooperatively so the same event loop can keep drawing and receiving keys.
The synchronous permission API remains available to startup and other synchronous
callers. `SessionPermissionHandler` owns both paths and applies either answer to the
same in-memory session grants; the terminal presents the decision but does not own
or persist authority. Tool existence and argument validation still happen before
the request reaches approval. Denial, dismissal, cancellation, or a missing handler
grants nothing and prevents the requested effect.

This is intentionally *not* keyword screening. The approval gates the capability,
not individual commands; there is no command parser to defeat.

## Verification

`tests/infra/tools/test_bash.py` covers the process guarantees: group cleanup,
bounded collection, output limits, decoding, exit codes, and the environment
contract. `tests/runtime/test_network_permissions.py` covers the approval
contract end to end, including that a denied or unapproved shell never executes.

```sh
uv run pytest -q tests/infra/tools/test_bash.py tests/runtime/test_network_permissions.py
```

## Related

- [Offline coding task E2E](offline-coding-e2e.md) — end-to-end task acceptance.
- [GitHub Issue workflows](github-workflow.md) — retained worktrees and the
  repository boundaries the file tools protect.
