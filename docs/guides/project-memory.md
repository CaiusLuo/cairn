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

## Evidence and checkout applicability

Public `accept`, `replace` and `revalidate` are asynchronous: supply an
`EvidenceResolver`, `EvidenceSource`, exact `EvidenceBinding`, and approving actor.
The resolver verifies actual Git objects and compares their declared dependencies
with descriptor-relative no-follow reads of the current Workspace. V1 requires a
Git repository-root Workspace for evidence; nested workspaces return unknown on
reads and reject new evidence, rather than guessing path coordinates. Non-Git and
unborn repositories are unsupported by V1. Source commits and evidence trees must
be full SHA-1/SHA-256 IDs. The evidence object is always explicitly typed as a tree.

For code inspection, the human reviews the actual scoped tree content. For
verification/review evidence, the host supplies the original TaskResult,
GitSnapshot, VerificationResult and optionally ReviewResult. It must additionally
supply independently known `CheckEvidence.scope` / `review_scope`; existing check
IDs do not establish scope. Each selected check must cover the full declared
claim scope; no inference of arbitrary semantic entailment is made. These inputs
must originate in the trusted workflow, never from coding-model JSON. The human
approval callback sees the resolved capsule and explicitly approves its binding
to the statement. Dependencies/HEAD are checked again after human approval.

Verification/review TaskResults must belong to the inspected Workspace: canonical
workspace paths must match, and a supplied repository root must match the inspected
Git root. Equal commit/tree IDs do not authorize mixing sibling worktree or clone
results. Canonical aliases of the same Workspace are allowed.

`source_head_oid` records origin A; `evidence_oid` is the actually inspected staged
tree B; `checkout_head_oid` records the checkout revision at approval. B need not
equal A's tree. Immutable capsules retain typed source/check/review identifiers,
scopes, and hashes. Losing an ephemeral worktree, trace or old Git tree does not
erase this provenance. New acceptance still requires independently available
source objects; a bare trace ID cannot stand in for them.

Freshness is computed per checkout and never saved as global lifecycle state:

- **fresh**: all declared dependencies match an attestation at this HEAD.
- **potentially_stale**: dependencies match, but HEAD has changed.
- **stale**: a declared dependency's content, existence, type or mode changed.
- **unknown**: incomplete/manual scope, IO failure, unsupported context or limits.

Explicit `revalidate` resolves new evidence and obtains another human approval,
appending an attestation without deleting previous provenance. Initial acceptance
checks the Candidate's original Task/Trace IDs against its source. Revalidation
may use a different Task/Trace, or code inspection without a TaskResult, while
preserving the original Candidate and all prior attestations. Each new attestation
records its own independently validated source, scopes and fingerprints.
Complete exact-path scope is a human assertion about dependency completeness,
not something hashes can prove. Incomplete/manual scopes are never automatically
fresh. Broad architectural
claims must not be marked complete on the basis of a few convenient file hashes.

File checks include SHA-256, POSIX permission mode and existence. Symlinks are
observed as type changes and never followed or accepted as content evidence.
Directory-membership scope covers immediate names, types and Git-visible modes
(including executability), not recursive contents; list content dependencies
separately as exact-path claims. Directory permissions are also recorded. Limits
are 16 paths, 64 KiB/file, 512 KiB aggregate across both descriptor walks, 256
members/directory and a two-second cooperative local IO deadline; each Git command
also has the existing bounded subprocess timeout/output limit. V1 assumes local
filesystems: it cannot forcibly time out a kernel-blocked filesystem syscall.
Observations and post-approval checks detect drift but cannot lock an entire
checkout against unrelated processes changing it after the final observation.

## Optional retrieval and request admission

The default Agent and CLI do not enable memory. The trusted host constructs a
`MemoryReader(store, EvidenceResolver(workspace, project_id))` and supplies it as
`build_agent(..., memory_reader=reader)`. The reader and repository context must
use the same Workspace, even when their worktrees share a project store. Direct
Agent integrations without a repository provider must supply the correct reader
Workspace themselves. No memory mutation API is registered as a model tool.

Use `related_paths` and `kinds` on the reader for explicit task-local filtering.
Each generation retrieves again, scoring path relationships, kinds and lexical
relevance deterministically. ASCII words and Chinese bigrams are supported; this
is not semantic search. The defaults inspect at most 16 ranked candidates and
return at most five fresh accepted entries / 8 KiB of escaped JSON. No relevant
entry is an ordinary empty result. Unavailable/corrupt storage and bounded read
failures produce an empty optional context; cancellation propagates.

`ContextBuilder` first constructs the normal baseline request. It admits whole
memory items only within the remaining complete-request budget and the optional
1,024-token memory allowance (including framing overhead). Memory never causes
additional history trimming. The fixed policy describes historical data as
lower trust; statements are JSON-escaped in a separate user-role data message,
not interpolated into system instructions. Each fallback model repeats admission
with its own builder and token counter. `AgentState.messages` is unchanged.

Trace metadata contains IDs, counts, freshness outcomes, read-error flags and
estimated request/token costs, never memory statement bodies. This boundary does
not make an LLM immune to adversarial text: ordinary tool validation, permission
checks and filesystem isolation remain the authority boundary. Enabling memory
does not change these permissions.

V1 intentionally exposes host Python APIs rather than a CLI approval UI. A host
must display the statement, full scope and resolved evidence binding to the human
inside its approval callback. Candidates that specify original task/trace IDs
retain them unchanged across revalidation; per-attestation identifiers describe
the evidence used for each separate approval.

See [implementation comparison and recommendations](../research/project-memory-research.md)
for the source-backed learn-cc, Codex and pi comparison.
