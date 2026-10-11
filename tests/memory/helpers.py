from pathlib import Path

from cairn.memory.models import (
    Attestation,
    Candidate,
    ClaimScope,
    EvidenceBinding,
    EvidenceCapsule,
    Fingerprint,
    Kind,
    Origin,
    PathKind,
    ScopeKind,
)
from cairn.memory.service import ApprovalAuthority, MemoryService
from cairn.memory.store import MemoryStore
from tests.git.helpers import git

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
