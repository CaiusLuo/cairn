"""Host-only mutation API. Never register this service/authority with ToolRegistry."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from cairn.memory.models import (
    Attestation,
    Candidate,
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

    def _accept(
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
        self.authority.require(
            ApprovalRequest(
                "replace" if replaces else "accept", entry, attestation, replaces
            )
        )
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

    def accept(
        self, entry_id: str, expected_version: int, attestation: Attestation
    ) -> MemoryEntry:
        return self._accept(
            self._get(entry_id, expected_version, Lifecycle.CANDIDATE), attestation
        )

    def replace(
        self,
        old_id: str,
        old_version: int,
        candidate_id: str,
        candidate_version: int,
        attestation: Attestation,
    ) -> MemoryEntry:
        old = self._get(old_id, old_version, Lifecycle.ACCEPTED)
        candidate = self._get(candidate_id, candidate_version, Lifecycle.CANDIDATE)
        return self._accept(candidate, attestation, old)

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
