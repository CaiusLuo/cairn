import asyncio
import dataclasses
from pathlib import Path

import pytest

from cairn.memory.evidence import EvidenceResolver, EvidenceSource
from cairn.memory.identity import ProjectIdentity
from cairn.memory.models import Candidate, CheckEvidence, Freshness, MemoryError, Origin
from cairn.memory.service import ApprovalAuthority, ApprovalRequest, MemoryService
from cairn.memory.store import MemoryStore
from cairn.repository import RepositoryEvidence
from cairn.review.models import ReviewResult, ReviewStatus
from cairn.tasks.models import TaskResult, TaskStatus
from cairn.workflow.models import GitSnapshot, VerificationCheck, VerificationResult
from cairn.workspace.workspace import Workspace
from tests.git.helpers import git
from tests.memory.helpers import candidate
from tests.memory.test_evidence import accept, binding, setup, status


def workflow_source(
    repo: Path,
    claim: Candidate,
    *,
    task_id: str = "task-a",
    trace_id: str = "a" * 32,
    check_id: str = "contract-a",
    origin: Origin = Origin.VERIFICATION,
) -> EvidenceSource:
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "write-tree")
    return EvidenceSource(
        source_head_oid=head,
        tree_oid=tree,
        task=TaskResult(
            task_id=task_id,
            trace_id=trace_id,
            status=TaskStatus.COMPLETED,
            repository=RepositoryEvidence(
                workspace_root=repo, repository_root=repo, head_revision=head
            ),
        ),
        snapshot=GitSnapshot(head, tree, "main", claim.scope.paths),
        verification=VerificationResult(tree, (VerificationCheck(check_id, True),)),
        check_scopes=(CheckEvidence(check_id=check_id, scope=claim.scope),),
        review=ReviewResult(
            status=ReviewStatus.COMPLETED, reviewed_tree=tree, trace_id="c" * 32
        )
        if origin is Origin.REVIEW
        else None,
        review_scope=claim.scope if origin is Origin.REVIEW else None,
    )


@pytest.mark.parametrize("origin", [Origin.VERIFICATION, Origin.REVIEW, Origin.CODE])
def test_cross_task_revalidation_retains_original_provenance(
    tmp_path: Path, origin: Origin
) -> None:
    repo, api, resolver = setup(tmp_path)
    approvals: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return True

    api.authority = ApprovalAuthority(approve)
    claim = candidate().model_copy(
        update={"source_task_id": "task-a", "trace_id": "a" * 32}
    )
    original = accept(
        api,
        resolver,
        claim,
        workflow_source(repo, claim),
        binding(claim, Origin.VERIFICATION, ("contract-a",)),
    )
    original_json = original.model_dump_json()
    git(repo, "commit", "--allow-empty", "-m", "unrelated")
    (repo / "parser.py").write_text("VERSION = 2\n")
    git(repo, "add", "parser.py")
    fresh_source = workflow_source(
        repo,
        claim,
        task_id="task-b",
        trace_id="b" * 32,
        check_id="contract-b",
        origin=origin,
    )
    if origin is Origin.CODE:
        fresh_source = EvidenceSource(
            fresh_source.source_head_oid, fresh_source.tree_oid
        )
    updated = asyncio.run(
        api.revalidate(
            original.id,
            original.version,
            resolver=resolver,
            source=fresh_source,
            binding=binding(
                claim, origin, () if origin is Origin.CODE else ("contract-b",)
            ),
            actor="human",
        )
    )
    assert updated.candidate == claim == original.candidate
    assert original.model_dump_json() == original_json
    assert updated.attestations[:-1] == original.attestations
    assert updated.version == original.version + 1
    assert len(updated.attestations) == 2
    old, new = (attestation.capsule for attestation in updated.attestations)
    assert (old.source_task_id, old.trace_id) == ("task-a", "a" * 32)
    assert (new.source_task_id, new.trace_id) == (
        (None, None) if origin is Origin.CODE else ("task-b", "b" * 32)
    )
    assert new.source_head_oid == fresh_source.source_head_oid != old.source_head_oid
    assert new.evidence_oid == fresh_source.tree_oid != old.evidence_oid
    assert new.evidence_oid != git(repo, "rev-parse", "HEAD^{tree}")
    assert new.dependencies != old.dependencies
    assert new.checks == fresh_source.check_scopes
    assert new.passed_check_ids == (() if origin is Origin.CODE else ("contract-b",))
    assert new.review_scope == fresh_source.review_scope
    assert new.review_trace_id == ("c" * 32 if origin is Origin.REVIEW else None)
    assert [request.operation for request in approvals] == ["accept", "revalidate"]
    assert approvals[-1].entry == original
    assert approvals[-1].attestation == updated.attestations[-1]
    assert api.inspect(updated.id) == updated
    assert status(resolver, updated) is Freshness.FRESH


@pytest.mark.parametrize("mismatch", ["task", "trace", "missing_task"])
def test_initial_acceptance_rejects_original_provenance_mismatch(
    tmp_path: Path, mismatch: str
) -> None:
    repo, api, resolver = setup(tmp_path)
    claim = candidate().model_copy(
        update={"source_task_id": "task-a", "trace_id": "a" * 32}
    )
    evidence = workflow_source(repo, claim)
    task = evidence.task
    assert task is not None
    if mismatch == "missing_task":
        evidence = EvidenceSource(evidence.source_head_oid, evidence.tree_oid)
        bound = binding(claim)
    else:
        evidence = dataclasses.replace(
            evidence,
            task=task.model_copy(
                update={"task_id": "task-b"}
                if mismatch == "task"
                else {"trace_id": "b" * 32}
            ),
        )
        bound = binding(claim, Origin.VERIFICATION, ("contract-a",))
    api.authority = ApprovalAuthority(lambda _: pytest.fail("Unexpected approval"))
    entry = api.propose(claim)
    with pytest.raises(MemoryError, match="task provenance mismatch"):
        asyncio.run(
            api.accept(
                entry.id,
                entry.version,
                resolver=resolver,
                source=evidence,
                binding=bound,
                actor="human",
            )
        )
    assert api.inspect(entry.id) == entry


@pytest.mark.parametrize("origin", [Origin.VERIFICATION, Origin.REVIEW])
@pytest.mark.parametrize("other_kind", ["worktree", "clone"])
@pytest.mark.parametrize("operation", ["accept", "revalidate"])
def test_rejects_task_from_another_checkout_with_identical_revisions(
    tmp_path: Path, origin: Origin, other_kind: str, operation: str
) -> None:
    repo, _, _ = setup(tmp_path)
    other = tmp_path / "other"
    if other_kind == "worktree":
        git(repo, "worktree", "add", "--detach", str(other), "HEAD")
    else:
        git(tmp_path, "clone", "--no-local", str(repo), str(other))
    workspace = Workspace(other)
    identity = asyncio.run(ProjectIdentity.inspect(workspace))
    resolver = EvidenceResolver(workspace, identity.project_id)
    api = MemoryService(
        MemoryStore(
            tmp_path / "other-store", identity.project_id, forbidden_roots=(repo, other)
        ),
        ApprovalAuthority(lambda _: True),
    )
    claim = candidate()
    local = workflow_source(other, claim, origin=origin)
    foreign = workflow_source(repo, claim, origin=origin)
    assert (local.source_head_oid, local.tree_oid) == (
        foreign.source_head_oid,
        foreign.tree_oid,
    )
    mixed = dataclasses.replace(local, task=foreign.task)
    bound = binding(claim, origin, ("contract-a",))
    entry = (
        accept(api, resolver, claim, local, bound)
        if operation == "revalidate"
        else api.propose(claim)
    )
    api.authority = ApprovalAuthority(lambda _: pytest.fail("Unexpected approval"))
    mutate = api.revalidate if operation == "revalidate" else api.accept
    with pytest.raises(MemoryError, match="task workspace mismatch"):
        asyncio.run(
            mutate(
                entry.id,
                entry.version,
                resolver=resolver,
                source=mixed,
                binding=bound,
                actor="human",
            )
        )
    assert api.inspect(entry.id) == entry


@pytest.mark.parametrize("fault", ["failed_check", "wrong_tree", "wrong_scope"])
def test_revalidation_requires_independently_valid_new_evidence(
    tmp_path: Path, fault: str
) -> None:
    repo, api, resolver = setup(tmp_path)
    claim = candidate().model_copy(
        update={"source_task_id": "task-a", "trace_id": "a" * 32}
    )
    entry = accept(
        api,
        resolver,
        claim,
        workflow_source(repo, claim),
        binding(claim, Origin.VERIFICATION, ("contract-a",)),
    )
    evidence = workflow_source(repo, claim, task_id="task-b", trace_id="b" * 32)
    if fault == "wrong_scope":
        evidence = dataclasses.replace(
            evidence,
            check_scopes=(
                CheckEvidence(
                    check_id="contract-a",
                    scope=claim.scope.model_copy(update={"paths": ("unrelated.py",)}),
                ),
            ),
        )
    else:
        evidence = dataclasses.replace(
            evidence,
            verification=VerificationResult(
                "f" * 40 if fault == "wrong_tree" else evidence.tree_oid,
                (VerificationCheck("contract-a", fault != "failed_check"),),
            ),
        )
    api.authority = ApprovalAuthority(lambda _: pytest.fail("Unexpected approval"))
    with pytest.raises(MemoryError, match=r"workflow evidence|claim scope"):
        asyncio.run(
            api.revalidate(
                entry.id,
                entry.version,
                resolver=resolver,
                source=evidence,
                binding=binding(claim, Origin.VERIFICATION, ("contract-a",)),
                actor="human",
            )
        )
    assert api.inspect(entry.id) == entry


@pytest.mark.parametrize("origin", [Origin.VERIFICATION, Origin.REVIEW])
@pytest.mark.parametrize("reported_root", ["canonical_alias", "absent", "mismatch"])
def test_canonical_same_workspace_and_optional_repository_root(
    tmp_path: Path, origin: Origin, reported_root: str
) -> None:
    repo, api, resolver = setup(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    claim = candidate()
    evidence = workflow_source(repo, claim, origin=origin)
    assert evidence.task is not None
    evidence = dataclasses.replace(
        evidence,
        task=evidence.task.model_copy(
            update={
                "repository": dataclasses.replace(
                    evidence.task.repository,
                    workspace_root=alias,
                    repository_root={
                        "canonical_alias": alias,
                        "absent": None,
                        "mismatch": tmp_path,
                    }[reported_root],
                )
            }
        ),
    )
    if reported_root == "mismatch":
        with pytest.raises(MemoryError, match="task workspace mismatch"):
            accept(
                api, resolver, claim, evidence, binding(claim, origin, ("contract-a",))
            )
    else:
        entry = accept(
            api, resolver, claim, evidence, binding(claim, origin, ("contract-a",))
        )
        assert status(resolver, entry) is Freshness.FRESH
        assert entry.attestations[0].capsule.checks == evidence.check_scopes
