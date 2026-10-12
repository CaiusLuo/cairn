"""Checkout-relative applicability; never modify global lifecycle or attestations."""

from dataclasses import dataclass

from cairn.core.memory.models import Freshness, MemoryEntry, MemoryError, ScopeKind
from cairn.runtime.memory.evidence import EvidenceResolver


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: Freshness
    reason: str  # Only fixed reason codes are returned, never arbitrary IO errors.
    attestation_index: int | None = None


class FreshnessChecker:
    def __init__(self, resolver: EvidenceResolver) -> None:
        self.resolver = resolver

    async def check(self, entry: MemoryEntry) -> FreshnessResult:
        scope = entry.candidate.scope
        if (
            not scope.complete
            or scope.kind is ScopeKind.MANUAL_RECHECK
            or not entry.attestations
        ):
            return FreshnessResult(Freshness.UNKNOWN, "incomplete_scope")
        if entry.project_id != self.resolver.project_id:
            return FreshnessResult(Freshness.UNKNOWN, "project_mismatch")
        try:
            identity = await self.resolver.identity()
            if identity.workspace_prefix:
                return FreshnessResult(Freshness.UNKNOWN, "nested_workspace")
            head = await self.resolver.head()
            dependencies = await self.resolver.capture(scope.paths)
            if head != await self.resolver.head():
                return FreshnessResult(Freshness.UNKNOWN, "checkout_changed")
            matching = [
                (i, a)
                for i, a in enumerate(entry.attestations)
                if a.capsule.dependencies == dependencies
            ]
            if not matching:
                return FreshnessResult(Freshness.STALE, "dependencies_changed")
            for index, attestation in reversed(matching):
                if attestation.checkout_head_oid == head:
                    return FreshnessResult(
                        Freshness.FRESH, "attested_scope_matches", index
                    )
            return FreshnessResult(Freshness.POTENTIALLY_STALE, "revision_changed")
        except (MemoryError, OSError, ValueError):
            return FreshnessResult(Freshness.UNKNOWN, "evidence_unavailable")
