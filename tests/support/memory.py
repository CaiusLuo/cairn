from pathlib import Path

from cairn.core.memory.models import (
    Attestation,
    Candidate,
    ClaimScope,
    EvidenceBinding,
    EvidenceCapsule,
    Fingerprint,
    Kind,
    Lifecycle,
    MemoryEntry,
    Origin,
    PathKind,
    ScopeKind,
)
from cairn.infra.memory.store import MemoryStore
from cairn.runtime.memory.service import (
    ApprovalAuthority,
    ApprovalRequest,
    MemoryService,
)
from tests.support.git import git

PROJECT = "a" * 64


def candidate(
    statement: str = "Validate the parser with its focused contract tests.",
) -> Candidate:
    return Candidate(
        statement=statement,
        kind=Kind.CONTRACT,
        scope=ClaimScope(
            kind=ScopeKind.EXACT_PATHS, paths=("parser.py",), complete=True
        ),
    )


def attestation(claim: Candidate) -> Attestation:
    """Synthetic host evidence for isolated transaction tests, not integration proof."""
    return Attestation(
        capsule=EvidenceCapsule(
            binding=EvidenceBinding(
                candidate_digest=claim.digest(), scope=claim.scope, origin=Origin.CODE
            ),
            source_head_oid="a" * 40,
            evidence_oid="b" * 40,
            dependencies=(
                Fingerprint(
                    path="parser.py", kind=PathKind.FILE, mode=0o644, sha256="c" * 64
                ),
            ),
        ),
        checkout_head_oid="a" * 40,
        actor="human",
        approved_at=1,
    )


def service(root: Path, *, approved: bool = True) -> MemoryService:
    return MemoryService(
        MemoryStore(root / "storage", PROJECT, forbidden_roots=(root / "work",)),
        ApprovalAuthority(lambda _: approved),
    )


def repository(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "parser.py").write_text("VERSION = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def accept_synthetic(
    api: MemoryService, entry_id: str, version: int, evidence: Attestation
) -> MemoryEntry:
    """Test ONLY the internal storage transaction; public acceptance resolves real Git."""
    entry = api._get(entry_id, version, Lifecycle.CANDIDATE)
    api.authority.require(ApprovalRequest("accept", entry, evidence))
    return api._commit_accept(entry, evidence)


def replace_synthetic(
    api: MemoryService,
    old_id: str,
    old_version: int,
    candidate_id: str,
    candidate_version: int,
    evidence: Attestation,
) -> MemoryEntry:
    old = api._get(old_id, old_version, Lifecycle.ACCEPTED)
    entry = api._get(candidate_id, candidate_version, Lifecycle.CANDIDATE)
    api.authority.require(ApprovalRequest("replace", entry, evidence, old))
    return api._commit_accept(entry, evidence, old)
