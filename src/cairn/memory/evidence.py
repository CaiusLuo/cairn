"""Resolve host-owned evidence against actual Git objects and materialized files."""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass

from pydantic import ValidationError

from cairn.memory.filesystem import FileObserver, ReadLimits, membership_digest
from cairn.memory.identity import ProjectIdentity, git_read
from cairn.memory.models import (
    Attestation,
    Candidate,
    CheckEvidence,
    ClaimScope,
    EvidenceBinding,
    EvidenceCapsule,
    Fingerprint,
    MemoryError,
    Origin,
    PathKind,
    ScopeKind,
)
from cairn.review.models import ReviewResult, ReviewSeverity, ReviewStatus
from cairn.tasks.models import TaskResult, TaskStatus
from cairn.workflow.models import GitSnapshot, VerificationResult
from cairn.workspace.workspace import Workspace

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Input from a trusted host, never a model response or arbitrary JSON report.

    CODE claims are human inspections of independently readable tree content.
    Verification/review additionally require actual caller-owned workflow results.
    No natural-language entailment is inferred from a passing check.
    """

    source_head_oid: str | None
    tree_oid: str
    task: TaskResult | None = None
    snapshot: GitSnapshot | None = None
    verification: VerificationResult | None = None
    review: ReviewResult | None = None
    check_scopes: tuple[CheckEvidence, ...] = ()
    review_scope: ClaimScope | None = None


class EvidenceResolver:
    def __init__(
        self, workspace: Workspace, project_id: str, limits: ReadLimits | None = None
    ) -> None:
        self.workspace = workspace
        self.project_id = project_id
        self.observer = FileObserver(workspace.root, limits)

    async def head(self) -> str:
        head = (
            (await git_read(self.workspace, "rev-parse", "--verify", "HEAD^{commit}"))
            .decode()
            .strip()
        )
        if not _OID.fullmatch(head):
            raise MemoryError("Memory checkout revision unavailable")
        return head

    async def identity(self) -> ProjectIdentity:
        identity = await ProjectIdentity.inspect(self.workspace)
        if identity.project_id != self.project_id:
            raise MemoryError("Memory project mismatch")
        return identity

    async def capture(self, paths: tuple[str, ...]) -> tuple[Fingerprint, ...]:
        # IO is capped; yield before and after so cancellation is never converted to unknown.
        await asyncio.sleep(0)
        result = self.observer.capture(paths)
        await asyncio.sleep(0)
        return result

    def _validate_source(
        self, candidate: Candidate, binding: EvidenceBinding, source: EvidenceSource
    ) -> None:
        if (
            binding.candidate_digest != candidate.digest()
            or binding.scope != candidate.scope
        ):
            raise MemoryError("Evidence binding does not cover this claim")
        if not _OID.fullmatch(source.tree_oid) or (
            source.source_head_oid is not None
            and not _OID.fullmatch(source.source_head_oid)
        ):
            raise MemoryError("Invalid memory evidence revision")
        if (
            candidate.source_task_id is not None or candidate.trace_id is not None
        ) and (
            source.task is None
            or (
                candidate.source_task_id,
                candidate.trace_id,
            )
            != (source.task.task_id, source.task.trace_id)
        ):
            raise MemoryError("Memory task provenance mismatch")
        if binding.origin is Origin.CODE:
            if any(x is not None for x in (source.verification, source.review)):
                raise MemoryError("Code evidence cannot claim verification")
            return
        task, snapshot, verification = source.task, source.snapshot, source.verification
        if task is None or snapshot is None or verification is None:
            raise MemoryError("Independent workflow evidence required")
        verification.validate()
        if (
            task.status is not TaskStatus.COMPLETED
            or task.repository.head_revision != source.source_head_oid
            or snapshot.head_revision != source.source_head_oid
            or snapshot.tree_revision != source.tree_oid
            or verification.tree_revision != source.tree_oid
            or not verification.passed
        ):
            raise MemoryError("Memory workflow evidence mismatch")
        available = {check.check_id for check in verification.checks if check.passed}
        if not set(binding.check_ids).issubset(available):
            raise MemoryError("Selected evidence check unavailable")
        scopes = {item.check_id: item for item in source.check_scopes}
        if len(scopes) != len(source.check_scopes) or len(scopes) > 16:
            raise MemoryError("Invalid check evidence scopes")
        for check_id in binding.check_ids:
            check = scopes.get(check_id)
            if (
                check is None
                or not check.scope.complete
                or check.scope.kind != candidate.scope.kind
                or not set(candidate.scope.paths).issubset(check.scope.paths)
            ):
                raise MemoryError("Check evidence does not cover claim scope")
        if binding.origin is Origin.REVIEW and (
            source.review is None
            or source.review_scope is None
            or not source.review_scope.complete
            or source.review_scope.kind != candidate.scope.kind
            or not set(candidate.scope.paths).issubset(source.review_scope.paths)
            or source.review.status is not ReviewStatus.COMPLETED
            or source.review.reviewed_tree != source.tree_oid
            or any(f.severity is ReviewSeverity.BLOCKER for f in source.review.findings)
        ):
            raise MemoryError("Memory review evidence unavailable")

    async def _matches_tree(
        self, tree: str, prefix: str, observed: Fingerprint, scope: ScopeKind
    ) -> None:
        path = prefix + observed.path
        record = await git_read(self.workspace, "ls-tree", "-z", tree, "--", path)
        if not record:
            if observed.kind is not PathKind.MISSING:
                raise MemoryError("Materialized dependency differs from verified tree")
            return
        fields = record.rstrip(b"\0").split(b"\t")
        if len(fields) != 2 or fields[1].decode("utf-8") != path:
            raise MemoryError("Memory tree dependency unavailable")
        mode, object_kind, oid = fields[0].decode("ascii").split()
        if (
            observed.kind is PathKind.FILE
            and object_kind == "blob"
            and mode in ("100644", "100755")
        ):
            data = await git_read(self.workspace, "cat-file", "blob", oid)
            if (
                len(data) > self.observer.limits.file_bytes
                or hashlib.sha256(data).hexdigest() != observed.sha256
                or (mode == "100755") != bool(observed.mode & 0o111)
            ):
                raise MemoryError("Materialized dependency differs from verified tree")
        elif (
            observed.kind is PathKind.DIRECTORY
            and object_kind == "tree"
            and scope is ScopeKind.DIRECTORY_MEMBERSHIP
        ):
            listing = await git_read(self.workspace, "ls-tree", "-z", oid)
            members: list[tuple[str, str]] = []
            for item in listing.split(b"\0"):
                if not item:
                    continue
                metadata, name = item.split(b"\t", 1)
                entry_mode = metadata.split(b" ", 1)[0].decode("ascii")
                members.append((name.decode("utf-8"), entry_mode))
                if len(members) > self.observer.limits.members:
                    raise MemoryError("Memory directory limit exceeded")
            if (
                membership_digest(members) != observed.sha256
                or len(members) != observed.members
            ):
                raise MemoryError("Materialized membership differs from verified tree")
        else:
            raise MemoryError("Evidence type does not cover declared scope")

    async def resolve(
        self,
        candidate: Candidate,
        binding: EvidenceBinding,
        source: EvidenceSource,
        actor: str,
    ) -> Attestation:
        try:
            self._validate_source(candidate, binding, source)
            identity = await self.identity()
            # Dependencies have repository-relative coordinates, stable across worktrees.
            if identity.workspace_prefix:
                raise MemoryError(
                    "Memory evidence requires a repository-root Workspace"
                )
            head = await self.head()
            if (
                await git_read(self.workspace, "cat-file", "-t", source.tree_oid)
                != b"tree\n"
            ):
                raise MemoryError("Evidence must identify an actual Git tree")
            if (
                source.source_head_oid is not None
                and await git_read(
                    self.workspace, "cat-file", "-t", source.source_head_oid
                )
                != b"commit\n"
            ):
                raise MemoryError("Source HEAD must identify an actual Git commit")
            dependencies = await self.capture(candidate.scope.paths)
            for dependency in dependencies:
                await self._matches_tree(
                    source.tree_oid, "", dependency, candidate.scope.kind
                )
            capsule = EvidenceCapsule(
                binding=binding,
                source_task_id=source.task.task_id if source.task else None,
                trace_id=source.task.trace_id if source.task else None,
                source_head_oid=source.source_head_oid,
                evidence_oid=source.tree_oid,
                passed_check_ids=binding.check_ids,
                checks=tuple(
                    next(c for c in source.check_scopes if c.check_id == i)
                    for i in binding.check_ids
                ),
                review_trace_id=source.review.trace_id if source.review else None,
                review_scope=source.review_scope
                if binding.origin is Origin.REVIEW
                else None,
                dependencies=dependencies,
            )
            attestation = Attestation(
                capsule=capsule,
                checkout_head_oid=head,
                actor=actor,
                approved_at=int(time.time()),
            )
            await self.confirm(attestation)
            return attestation
        except (ValueError, TypeError, ValidationError, UnicodeError):
            raise MemoryError("Memory evidence validation failed") from None

    async def confirm(self, attestation: Attestation) -> None:
        await self.identity()
        if (
            await self.head() != attestation.checkout_head_oid
            or await self.capture(attestation.capsule.binding.scope.paths)
            != attestation.capsule.dependencies
        ):
            raise MemoryError("Memory evidence changed before approval commit")
