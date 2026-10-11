# Project Memory V1

Memory stores human-approved project knowledge, not transcripts, user profiles,
task checkpoints or repository structure. It is disabled unless the host explicitly
supplies a memory reader to an Agent. There is no model-callable acceptance tool.

## Host authority and storage

The trusted Python host owns `MemoryService` and `ApprovalAuthority`. The approval
callback must obtain a human's decision on the exact claim, scope, evidence and
operation; a coding-model verdict is not human approval. Model JSON cannot supply
an authority. This is an API boundary, not protection from arbitrary same-user
Python, direct SQLite writes, disabled sandboxes or unconstrained tools.

`MemoryStore(directory, project_id, forbidden_roots=...)` requires the host to
enumerate **all** sandbox-writable roots (Workspace, effective TMPDIR, uv cache,
and any additional tool-specific roots). Keep storage outside those roots. It
does not promise confidentiality from tools that can read HOME. Private directory
permissions and symlink rejection are defense in depth, not same-user isolation.
Git common-directory identity shares storage across worktrees; separate clones
are distinct. Repository movement changes identity; V1 does not auto-migrate it.

SQLite uses WAL, FULL synchronization, bounded busy waits, transactions, schema
versioning and version CAS. Replacing a claim supersedes the original and accepts
the proposed replacement in one transaction. Deletion is a durable tombstone,
not forensic erasure; identical proposals do not resurrect deleted entries.
At most 256 entries including tombstones, 16 attestations per entry and 64 KiB per
record are allowed. V1 has no automatic pruning. Corruption/unknown schemas fail
explicitly without resetting data. Inspect via the typed API; direct DB editing
and exported data are not an approval interface.

Secrets: inputs are small and typed, never raw transcripts or command output.
Common credential patterns and explicitly supplied secret values are rejected
before transactions. Hosts must provide their known credential values through
`sensitive_values`; pattern detection cannot identify every arbitrary secret.
Do not log candidate text or unrestricted validation errors in host integrations.
