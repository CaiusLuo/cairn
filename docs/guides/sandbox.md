# Local command execution

Cairn is **local-first**. The `bash` tool runs a command as the invoking user, in
the Workspace directory, with the invoking user's environment. There is no OS
sandbox on any platform, and this document makes no isolation claim: a command
can read and write whatever the user can, including files outside the Workspace
and the network.

That is a deliberate change from the earlier sandbox-first model, which required
macOS `sandbox-exec` or Linux `bubblewrap` before a coding agent could run at all.
The trade is explicit: a local agent is useful on an ordinary machine, and the
operator — not a mount namespace — decides whether it may run a shell.

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

The file tools keep their own, independent boundary: `infra.workspace.paths`
rejects absolute paths, `..`, `.git` components and symlinked components, so
`read_file`/`write_file`/`edit_file`/`patch`/`glob`/`grep` cannot leave the
Workspace.

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
| Read (`read_file`, `grep`, `glob`) | Allowed, no approval |
| Write (`write_file`, `edit_file`, `patch_file`) | Needs a grant on the containing directory |

An approved directory covers its whole subtree for the rest of the session, so
approving `/tmp/data` once covers `/tmp/data/a.txt` and `/tmp/data/b.txt`. `.git`
stays unreachable wherever it appears.

Preflight checks only that the path is a well-formed target; the grant is enforced
when the tool executes, so a refusal is a tool error the model can act on rather
than an exception during approval.

## Approval

A local shell is a host-level capability, so it is asked for explicitly. The first
`bash` call in a session raises a permission request; approving "for this session"
trusts every later command until the session ends, and the grant is never written
to disk. Denial — or no interactive approval path — means nothing runs.

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
