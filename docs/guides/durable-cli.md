# M1-C.3 Durable CLI

The default `cairn` command remains stateless and opens no SessionStore.
Durable conversations are opt-in and use the existing DurableSession and
`run_turn` ownership boundary.

```bash
cairn session new
cairn session inspect SESSION_ID
cairn session reopen SESSION_ID
cairn session reconcile SESSION_ID
```

All four commands accept `--store DIRECTORY`; the default is
`~/.local/state/cairn/sessions`. Repeat `--forbid-root PATH` to exclude additional
host writable roots. The CLI also excludes the current Workspace, `/tmp`, the
effective Bash temporary directory and uv cache. SessionStore rejects overlapping
paths and unsupported storage environments. The database must remain outside
every Agent-writable root. Inspection opens an existing store read-only, creates
no Agent and requests no provider approval.

Creation, reopen and reconciliation require interactive, fresh provider approval
before credential resolution or provider runtime construction, including ENV
configuration. Reopen constructs a fresh Agent and permission handler; committed
conversation messages are restored, permission and network grants are not.
Trusted host observations pin canonical Workspace device/inode, Git repository,
common/admin directories, worktree registration, HEAD/branch, provider reference,
model group, prompt digest and budgets. Invalid registration, Workspace identity
or clone replacement fails closed. Git reads use the bounded isolated adapter;
the synchronous observer joins its worker before returning or propagating an
interrupt. No model or tool executes during reopen or reconciliation.

Reports show the Session ID, last committed checkpoint, recovery state, current
revision, unfinished Turn and bounded effect-fence ordinals. Fences mean external
effects may already have occurred; inspect surviving workspace and external work
before resolving them. Reports omit raw arguments, tool output and exception
details. Ordinary reopen refuses unfinished turns and directs the user to
`session reconcile`. No interrupted execution resumes automatically.

Reopen and reconciliation require the exact displayed token
`abandon_without_replay SESSION_ID TURN_ID_OR_none REVISION_VERSION`.
Changed Git HEAD/branch or provider binding is explicitly identified before this
confirmation. A changed observation during confirmation rejects the operation.
Refusal, EOF and noninteractive input never approve. The trusted HostResolution
uses that exact Session/Turn/revision and current host binding, and the runtime
performs its existing CAS reconciliation. `reconcile` exits after resolution;
`reopen` can then start new turns. Abandonment records resolution without replay
or rollback of effects that already happened.

Streaming text and tool progress are provisional. Only a successful
CheckpointReceipt produces `Committed checkpoint N (OUTCOME)`. Persistence
failure or uncertainty stops input admission. Cancellation settles owned turn
work before closing the session; repeated cancellation cannot release the writer
while cleanup is still running. Uncooperative providers/tools can delay shutdown.
In durable mode `/model` and `/provider` commands are rejected: exit, change
configuration, then reopen with fresh approval and explicit binding confirmation.

Offline regressions use scripted providers, real SQLite, subprocess interruption
and cancellation barriers. Application-crash recovery is not power-loss evidence.
SQLite **3.50.4 WAL-reset compatibility remains a release blocker**; this feature
does not upgrade SQLite or qualify deployment. The [Store release gate](session-store.md#verification-and-release-gate)
and [runtime limits](durable-session.md#verification-and-release-limits) still apply.
Task recovery, Worktree adoption and automatic replay remain outside this contract.
