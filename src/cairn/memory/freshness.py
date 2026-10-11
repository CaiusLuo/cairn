"""Checkout-relative applicability; never modify global lifecycle or attestations."""

from dataclasses import dataclass

from cairn.memory.evidence import EvidenceResolver
from cairn.memory.models import Freshness, MemoryEntry, MemoryError, ScopeKind


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: Freshness
    reason: str  # Only fixed reason codes are returned, never arbitrary IO errors.


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
                a for a in entry.attestations if a.capsule.dependencies == dependencies
            ]
            if not matching:
                return FreshnessResult(Freshness.STALE, "dependencies_changed")
            if any(a.checkout_head_oid == head for a in matching):
                return FreshnessResult(Freshness.FRESH, "attested_scope_matches")
            return FreshnessResult(Freshness.POTENTIALLY_STALE, "revision_changed")
        except (MemoryError, OSError, ValueError):
            return FreshnessResult(Freshness.UNKNOWN, "evidence_unavailable")
