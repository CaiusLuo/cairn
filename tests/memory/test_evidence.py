import asyncio
import dataclasses
import os
from pathlib import Path

import pytest

from cairn.memory.evidence import EvidenceResolver, EvidenceSource
from cairn.memory.filesystem import ReadLimits
from cairn.memory.freshness import FreshnessChecker
from cairn.memory.identity import ProjectIdentity
from cairn.memory.models import (
    Candidate,
    CheckEvidence,
    ClaimScope,
    EvidenceBinding,
    Freshness,
    Kind,
    Lifecycle,
    MemoryEntry,
    MemoryError,
    Origin,
    ScopeKind,
)
from cairn.memory.service import ApprovalAuthority, MemoryService
from cairn.memory.store import MemoryStore
from cairn.repository import RepositoryEvidence
from cairn.review.models import ReviewResult, ReviewStatus
from cairn.tasks.models import TaskResult, TaskStatus
from cairn.workflow.models import GitSnapshot, VerificationCheck, VerificationResult
from cairn.workspace.workspace import Workspace
from tests.git.helpers import git
from tests.memory.helpers import candidate, repository


def setup(root: Path) -> tuple[Path, MemoryService, EvidenceResolver]:
    repo = repository(root / "repo")
    workspace = Workspace(repo)
    identity = asyncio.run(ProjectIdentity.inspect(workspace))
    api = MemoryService(
        MemoryStore(root / "store", identity.project_id, forbidden_roots=(repo,)),
        ApprovalAuthority(lambda _: True),
    )
    return repo, api, EvidenceResolver(workspace, identity.project_id)


def source(repo: Path) -> EvidenceSource:
    return EvidenceSource(
        git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
    )


def binding(
    claim: Candidate, origin: Origin = Origin.CODE, checks: tuple[str, ...] = ()
) -> EvidenceBinding:
    return EvidenceBinding(
        candidate_digest=claim.digest(),
        scope=claim.scope,
        origin=origin,
        check_ids=checks,
    )


def accept(
    api: MemoryService,
    resolver: EvidenceResolver,
    claim: Candidate | None = None,
    evidence: EvidenceSource | None = None,
    bound: EvidenceBinding | None = None,
) -> MemoryEntry:
    claim = claim or candidate()
    entry = api.propose(claim)
    return asyncio.run(
        api.accept(
            entry.id,
            entry.version,
            resolver=resolver,
            source=evidence or source(resolver.workspace.root),
            binding=bound or binding(claim),
            actor="human",
        )
    )


def status(resolver: EvidenceResolver, entry: MemoryEntry) -> Freshness:
    return asyncio.run(FreshnessChecker(resolver).check(entry)).status


def test_accept_and_materialized_changes(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    assert status(resolver, entry) is Freshness.FRESH
    (repo / "parser.py").write_text("VERSION = 2\n")
    assert status(resolver, entry) is Freshness.STALE
    assert api.inspect(entry.id) == entry
    git(repo, "checkout", "--", "parser.py")
    assert status(resolver, entry) is Freshness.FRESH
    (repo / "parser.py").unlink()
    assert status(resolver, entry) is Freshness.STALE


@pytest.mark.parametrize("change", ["executable", "permission", "symlink", "directory"])
def test_type_mode_changes(tmp_path: Path, change: str) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    path = repo / "parser.py"
    if change in ("executable", "permission"):
        path.chmod(0o755 if change == "executable" else 0o600)
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(tmp_path / "outside")
        else:
            path.mkdir()
    assert status(resolver, entry) is Freshness.STALE


def test_unrelated_revision_requires_explicit_new_attestation(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    (repo / "unrelated").write_text("other\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unrelated")
    assert status(resolver, entry) is Freshness.POTENTIALLY_STALE
    updated = asyncio.run(
        api.revalidate(
            entry.id,
            entry.version,
            resolver=resolver,
            source=source(repo),
            binding=binding(entry.candidate),
            actor="human",
        )
    )
    assert updated.attestations[0] == entry.attestations[0]
    assert len(updated.attestations) == 2
    assert status(resolver, updated) is Freshness.FRESH


def test_head_a_verified_staged_tree_b_and_scoped_checks(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "parser.py").write_text("VERSION = 2\n")
    git(repo, "add", "parser.py")
    tree = git(repo, "write-tree")
    claim = candidate()
    task = TaskResult(
        task_id="task-1",
        status=TaskStatus.COMPLETED,
        repository=RepositoryEvidence(workspace_root=repo, head_revision=head),
    )
    snapshot = GitSnapshot(head, tree, "main", ("parser.py",))
    verification = VerificationResult(
        tree, (VerificationCheck("parser-contract", True),)
    )
    evidence = EvidenceSource(
        head,
        tree,
        task,
        snapshot,
        verification,
        check_scopes=(CheckEvidence(check_id="parser-contract", scope=claim.scope),),
    )
    entry = accept(
        api,
        resolver,
        claim,
        evidence,
        binding(claim, Origin.VERIFICATION, ("parser-contract",)),
    )
    capsule = entry.attestations[0].capsule
    assert capsule.source_head_oid == head
    assert capsule.evidence_oid == tree
    assert capsule.evidence_oid != git(repo, "rev-parse", "HEAD^{tree}")
    assert status(resolver, entry) is Freshness.FRESH


@pytest.mark.parametrize(
    "fault", ["missing", "wrong_tree", "failed", "narrow", "unrelated_id"]
)
def test_unproven_checks_cannot_authorize_claim(tmp_path: Path, fault: str) -> None:
    repo, api, resolver = setup(tmp_path)
    claim = candidate()
    base = source(repo)
    assert base.source_head_oid is not None
    task = TaskResult(
        task_id="task",
        status=TaskStatus.COMPLETED,
        repository=RepositoryEvidence(
            workspace_root=repo, head_revision=base.source_head_oid
        ),
    )
    verification = VerificationResult(
        "f" * 40 if fault == "wrong_tree" else base.tree_oid,
        (VerificationCheck("check", True),),
    )
    if fault == "failed":
        task.status = TaskStatus.RUNTIME_ERROR
    scope = (
        ClaimScope(kind=ScopeKind.EXACT_PATHS, paths=("unrelated.py",), complete=True)
        if fault == "narrow"
        else claim.scope
    )
    evidence = EvidenceSource(
        base.source_head_oid,
        base.tree_oid,
        task,
        GitSnapshot(base.source_head_oid, base.tree_oid, "main", ("parser.py",)),
        None if fault == "missing" else verification,
        check_scopes=(CheckEvidence(check_id="check", scope=scope),),
    )
    with pytest.raises(MemoryError):
        accept(
            api,
            resolver,
            claim,
            evidence,
            binding(
                claim,
                Origin.VERIFICATION,
                ("other" if fault == "unrelated_id" else "check",),
            ),
        )
    assert api.store.entries()[0].lifecycle is Lifecycle.CANDIDATE


def test_worktrees_share_store_but_not_applicability_and_capsule_survives(
    tmp_path: Path,
) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    other = tmp_path / "other"
    git(repo, "worktree", "add", "-b", "other", str(other))
    other_resolver = EvidenceResolver(Workspace(other), api.store.project_id)
    (other / "parser.py").write_text("different\n")
    assert status(other_resolver, entry) is Freshness.STALE
    assert status(resolver, entry) is Freshness.FRESH
    git(other, "checkout", "--", "parser.py")
    # No source worktree or transcript is required to compare retained evidence.
    git(repo, "worktree", "add", "-b", "temporary", str(tmp_path / "temporary"))
    temporary_resolver = EvidenceResolver(
        Workspace(tmp_path / "temporary"), api.store.project_id
    )
    second = accept(api, temporary_resolver, candidate("A second parser rule."))
    git(repo, "worktree", "remove", str(tmp_path / "temporary"))
    assert status(other_resolver, second) is Freshness.FRESH
    assert second.attestations[0].capsule.dependencies


def test_directory_membership_and_absence(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    directory = repo / "suite"
    directory.mkdir()
    (directory / "one.py").write_text("pass\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "suite")
    claim = Candidate(
        statement="The suite membership has been reviewed.",
        kind=Kind.TESTING,
        scope=ClaimScope(
            kind=ScopeKind.DIRECTORY_MEMBERSHIP, paths=("suite",), complete=True
        ),
    )
    entry = accept(api, resolver, claim)
    assert status(resolver, entry) is Freshness.FRESH
    (directory / "two.py").write_text("pass\n")
    assert status(resolver, entry) is Freshness.STALE
    negative = Candidate(
        statement="This scoped generated file is absent.",
        kind=Kind.WORKFLOW,
        scope=ClaimScope(
            kind=ScopeKind.EXACT_PATHS, paths=("missing/file",), complete=True
        ),
    )
    absent = accept(api, resolver, negative)
    assert status(resolver, absent) is Freshness.FRESH
    (repo / "missing").mkdir()
    (repo / "missing/file").write_text("now present")
    assert status(resolver, absent) is Freshness.STALE


@pytest.mark.parametrize(
    "scope_kind,complete",
    [(ScopeKind.MANUAL_RECHECK, True), (ScopeKind.EXACT_PATHS, False)],
)
def test_broad_incomplete_claims_never_automatically_fresh(
    tmp_path: Path, scope_kind: ScopeKind, complete: bool
) -> None:
    _, api, resolver = setup(tmp_path)
    claim = Candidate(
        statement="A broad architectural claim needing manual review.",
        kind=Kind.ARCHITECTURE,
        scope=ClaimScope(kind=scope_kind, paths=("parser.py",), complete=complete),
    )
    entry = accept(api, resolver, claim)
    assert status(resolver, entry) is Freshness.UNKNOWN


def test_symlink_ancestor_and_outside_content_never_followed(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    (repo / "nested").mkdir()
    (repo / "nested/file").write_text("inside\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "nested")
    claim = candidate().model_copy(
        update={
            "scope": ClaimScope(
                kind=ScopeKind.EXACT_PATHS, paths=("nested/file",), complete=True
            )
        }
    )
    entry = accept(api, resolver, claim)
    (repo / "nested/file").unlink()
    (repo / "nested").rmdir()
    (repo / "nested").symlink_to(tmp_path)
    (tmp_path / "file").write_text("inside\n")
    assert status(resolver, entry) is Freshness.STALE
    with pytest.raises(MemoryError):
        accept(
            api,
            resolver,
            candidate("Other rule").model_copy(update={"scope": claim.scope}),
        )


def test_io_failure_limits_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    limited = EvidenceResolver(
        resolver.workspace, api.store.project_id, ReadLimits(file_bytes=1)
    )
    assert status(limited, entry) is Freshness.UNKNOWN

    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError("password=must-not-escape")

    monkeypatch.setattr("cairn.memory.filesystem.os.open", denied)
    result = asyncio.run(FreshnessChecker(resolver).check(entry))
    assert result.status is Freshness.UNKNOWN
    assert "password" not in repr(result)
    monkeypatch.undo()

    async def cancelled() -> None:
        task = asyncio.create_task(FreshnessChecker(resolver).check(entry))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())


def test_evidence_changed_during_human_approval_is_rejected(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)

    def approve(_: object) -> bool:
        (repo / "parser.py").write_text("changed during review")
        return True

    api.authority = ApprovalAuthority(approve)
    with pytest.raises(MemoryError, match="changed"):
        accept(api, resolver)
    assert api.store.entries()[0].lifecycle is Lifecycle.CANDIDATE


def test_revision_and_binding_mismatch_rejected(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    with pytest.raises(MemoryError):
        accept(api, resolver, bound=binding(candidate("Other claim")))
    with pytest.raises(MemoryError):
        accept(
            api, resolver, evidence=dataclasses.replace(source(repo), tree_oid="f" * 40)
        )
    with pytest.raises(MemoryError):
        accept(
            api,
            resolver,
            evidence=dataclasses.replace(
                source(repo), tree_oid=git(repo, "rev-parse", "HEAD")
            ),
        )


def test_freshness_is_read_only(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    before = api.store.path.read_bytes()
    head = git(repo, "rev-parse", "HEAD")
    assert status(resolver, entry) is Freshness.FRESH
    assert api.store.path.read_bytes() == before
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == head


def test_review_requires_independent_explicit_scope(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    claim = candidate()
    base = source(repo)
    assert base.source_head_oid
    evidence = EvidenceSource(
        base.source_head_oid,
        base.tree_oid,
        TaskResult(
            task_id="task",
            status=TaskStatus.COMPLETED,
            trace_id="a" * 32,
            repository=RepositoryEvidence(
                workspace_root=repo, head_revision=base.source_head_oid
            ),
        ),
        GitSnapshot(base.source_head_oid, base.tree_oid, "main", ("parser.py",)),
        VerificationResult(base.tree_oid, (VerificationCheck("test", True),)),
        ReviewResult(
            status=ReviewStatus.COMPLETED,
            reviewed_tree=base.tree_oid,
            trace_id="b" * 32,
        ),
    )
    with pytest.raises(MemoryError, match="review evidence"):
        accept(api, resolver, claim, evidence, binding(claim, Origin.REVIEW))
    accepted = accept(
        api,
        resolver,
        claim,
        dataclasses.replace(evidence, review_scope=claim.scope),
        binding(claim, Origin.REVIEW),
    )
    capsule = accepted.attestations[0].capsule
    assert capsule.source_task_id == "task"
    assert capsule.trace_id == "a" * 32
    assert capsule.review_trace_id == "b" * 32
    assert capsule.review_scope == claim.scope


def test_status_truncation_is_not_used_as_proof(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    for i in range(65):
        (repo / f"a{i:03}").write_text("untracked")
    (repo / "parser.py").write_text("modified")
    assert status(resolver, entry) is Freshness.STALE


@pytest.mark.parametrize(
    "limit", [ReadLimits(total_bytes=1), ReadLimits(seconds=0.0000001)]
)
def test_aggregate_and_time_limits(tmp_path: Path, limit: ReadLimits) -> None:
    _, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    assert (
        status(EvidenceResolver(resolver.workspace, api.store.project_id, limit), entry)
        is Freshness.UNKNOWN
    )


def test_directory_limit_and_file_only_proof_rejected(tmp_path: Path) -> None:
    repo, api, resolver = setup(tmp_path)
    (repo / "suite").mkdir()
    for name in ("a", "b"):
        (repo / "suite" / name).write_text("content")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "suite")
    wrong = Candidate(
        statement="Directory-wide contract.",
        kind=Kind.CONTRACT,
        scope=ClaimScope(kind=ScopeKind.EXACT_PATHS, paths=("suite",), complete=True),
    )
    with pytest.raises(MemoryError, match="scope"):
        accept(api, resolver, wrong)
    scoped = wrong.model_copy(
        update={
            "scope": ClaimScope(
                kind=ScopeKind.DIRECTORY_MEMBERSHIP, paths=("suite",), complete=True
            )
        }
    )
    entry = accept(api, resolver, scoped)
    limited = EvidenceResolver(
        resolver.workspace, api.store.project_id, ReadLimits(members=1)
    )
    assert status(limited, entry) is Freshness.UNKNOWN


def test_no_follow_when_file_replaced_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, api, resolver = setup(tmp_path)
    entry = accept(api, resolver)
    outside = tmp_path / "outside"
    outside.write_text("VERSION = 1\n")
    original = os.open
    replaced = False

    def swap(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "parser.py" and not replaced:
            replaced = True
            (repo / "parser.py").unlink()
            (repo / "parser.py").symlink_to(outside)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("cairn.memory.filesystem.os.open", swap)
    assert status(resolver, entry) is Freshness.UNKNOWN
    assert replaced
