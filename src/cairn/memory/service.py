"""Host-only mutation API. Never register this service/authority with ToolRegistry."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from cairn.memory.evidence import EvidenceResolver, EvidenceSource
from cairn.memory.models import (
    Attestation,
    Candidate,
    EvidenceBinding,
    Lifecycle,
    MemoryEntry,
    MemoryError,
    VersionConflict,
)
from cairn.memory.store import MemoryStore


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    operation: str
    entry: MemoryEntry
    attestation: Attestation | None
    replaces: MemoryEntry | None = None


class ApprovalAuthority:
    """A trusted host callback representing explicit human approval.

    Not serializable approval data. A model-provided bool/actor cannot create it.
    Hosts must not implement the callback by asking the coding model to approve.
    """

    def __init__(self, decide: Callable[[ApprovalRequest], bool]) -> None:
        self._decide = decide

    def require(self, request: ApprovalRequest) -> None:
        try:
            approved = self._decide(request)
        except Exception:
            raise MemoryError("Memory approval unavailable") from None
        if approved is not True:
            raise MemoryError("Memory approval denied")


class MemoryService:
    def __init__(self, store: MemoryStore, authority: ApprovalAuthority) -> None:
        if not isinstance(authority, ApprovalAuthority):
            raise MemoryError("Host approval authority required")
        self.store = store
        self.authority = authority

    def propose(self, candidate: Candidate) -> MemoryEntry:
        return self.store._insert(
            MemoryEntry(
                id=uuid.uuid4().hex,
                project_id=self.store.project_id,
                version=1,
                created_at=int(time.time()),
                lifecycle=Lifecycle.CANDIDATE,
                candidate=candidate,
            )
        )

    def inspect(self, entry_id: str) -> MemoryEntry:
        return self.store.inspect(entry_id)

    def _get(self, entry_id: str, version: int, lifecycle: Lifecycle) -> MemoryEntry:
        entry = self.inspect(entry_id)
        if entry.version != version:
            raise VersionConflict()
        if entry.lifecycle is not lifecycle:
            raise MemoryError("Memory lifecycle does not allow operation")
        return entry

    def _commit_accept(
        self,
        entry: MemoryEntry,
        attestation: Attestation,
        replaces: MemoryEntry | None = None,
    ) -> MemoryEntry:
        accepted = entry.model_copy(
            update={
                "lifecycle": Lifecycle.ACCEPTED,
                "version": entry.version + 1,
                "attestations": (attestation,),
                "supersedes_id": replaces.id if replaces else None,
            }
        )
        self.store._serialize(
            accepted
        )  # Before approval and before opening a write transaction.
        updates = [(accepted, entry.version)]
        if replaces is not None:
            updates.append(
                (
                    replaces.model_copy(
                        update={
                            "lifecycle": Lifecycle.SUPERSEDED,
                            "version": replaces.version + 1,
                        }
                    ),
                    replaces.version,
                )
            )
        self.store._update(tuple(updates))
        return accepted

    async def _resolve_approval(
        self,
        entry: MemoryEntry,
        *,
        resolver: EvidenceResolver,
        source: EvidenceSource,
        binding: EvidenceBinding,
        actor: str,
        operation: str,
        replaces: MemoryEntry | None = None,
    ) -> Attestation:
        if resolver.project_id != self.store.project_id:
            raise MemoryError("Memory project mismatch")
        attestation = await resolver.resolve(
            entry.candidate,
            binding,
            source,
            actor,
            initial_acceptance=entry.lifecycle is Lifecycle.CANDIDATE,
        )
        prospective = entry.model_copy(
            update={
                "lifecycle": Lifecycle.ACCEPTED,
                "attestations": (*entry.attestations, attestation),
            }
        )
        self.store._serialize(prospective)
        self.authority.require(ApprovalRequest(operation, entry, attestation, replaces))
        # Human review may have taken time; inspect again before the transaction.
        await resolver.confirm(attestation)
        return attestation

    async def accept(
        self,
        entry_id: str,
        expected_version: int,
        *,
        resolver: EvidenceResolver,
        source: EvidenceSource,
        binding: EvidenceBinding,
        actor: str,
    ) -> MemoryEntry:
        entry = self._get(entry_id, expected_version, Lifecycle.CANDIDATE)
        attestation = await self._resolve_approval(
            entry,
            resolver=resolver,
            source=source,
            binding=binding,
            actor=actor,
            operation="accept",
        )
        return self._commit_accept(entry, attestation)

    async def replace(
        self,
        old_id: str,
        old_version: int,
        candidate_id: str,
        candidate_version: int,
        *,
        resolver: EvidenceResolver,
        source: EvidenceSource,
        binding: EvidenceBinding,
        actor: str,
    ) -> MemoryEntry:
        old = self._get(old_id, old_version, Lifecycle.ACCEPTED)
        entry = self._get(candidate_id, candidate_version, Lifecycle.CANDIDATE)
        attestation = await self._resolve_approval(
            entry,
            resolver=resolver,
            source=source,
            binding=binding,
            actor=actor,
            operation="replace",
            replaces=old,
        )
        return self._commit_accept(entry, attestation, old)

    async def revalidate(
        self,
        entry_id: str,
        expected_version: int,
        *,
        resolver: EvidenceResolver,
        source: EvidenceSource,
        binding: EvidenceBinding,
        actor: str,
    ) -> MemoryEntry:
        entry = self._get(entry_id, expected_version, Lifecycle.ACCEPTED)
        attestation = await self._resolve_approval(
            entry,
            resolver=resolver,
            source=source,
            binding=binding,
            actor=actor,
            operation="revalidate",
        )
        updated = entry.model_copy(
            update={
                "attestations": (*entry.attestations, attestation),
                "version": entry.version + 1,
            }
        )
        self.store._update(((updated, expected_version),))
        return updated

    def delete(self, entry_id: str, expected_version: int) -> MemoryEntry:
        old = self.inspect(entry_id)
        if old.version != expected_version:
            raise VersionConflict()
        if old.lifecycle is Lifecycle.DELETED:
            return old
        self.authority.require(ApprovalRequest("delete", old, None))
        deleted = old.model_copy(
            update={"lifecycle": Lifecycle.DELETED, "version": old.version + 1}
        )
        self.store._update(((deleted, old.version),))
        return deleted
