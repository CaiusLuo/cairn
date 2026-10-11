"""Deterministic bounded retrieval. No writes, model calls or tool authority."""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass

from cairn.core.memory.context import MemoryContext, MemoryContextItem, render_memory
from cairn.core.memory.models import Freshness, Kind, Lifecycle, MemoryEntry, safe_path
from cairn.infra.memory.store import MemoryStore
from cairn.runtime.memory.evidence import EvidenceResolver
from cairn.runtime.memory.freshness import FreshnessChecker

_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "please",
        "code",
        "project",
        "change",
        "use",
        "run",
    }
)


def _words(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]{3,}", text.lower())) - _STOP
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(phrase[i : i + 2] for i in range(len(phrase) - 1))
    return words


@dataclass(frozen=True, slots=True)
class RetrievalLimits:
    candidates: int = 16
    items: int = 5
    bytes: int = 8192
    tokens: int = 1024
    seconds: float = 5.0

    def __post_init__(self) -> None:
        if not (
            1 <= self.candidates <= 16
            and 1 <= self.items <= 5
            and 256 <= self.bytes <= 8192
            and 1 <= self.tokens <= 4096
            and 0 < self.seconds <= 10
        ):
            raise ValueError("Invalid memory retrieval limits")


class MemoryReader:
    def __init__(
        self,
        store: MemoryStore,
        resolver: EvidenceResolver,
        *,
        related_paths: tuple[str, ...] = (),
        kinds: tuple[Kind, ...] = (),
        limits: RetrievalLimits | None = None,
    ) -> None:
        if store.project_id != resolver.project_id:
            raise ValueError("Memory reader project mismatch")
        if len(related_paths) > 16 or len(kinds) > 4:
            raise ValueError("Memory query limit exceeded")
        for path in related_paths:
            safe_path(path)
        self.store = store
        self.resolver = resolver
        self.related_paths = related_paths
        self.kinds = kinds
        self.limits = limits or RetrievalLimits()

    def _score(self, entry: MemoryEntry, query: str) -> int:
        if entry.lifecycle is not Lifecycle.ACCEPTED or (
            self.kinds and entry.candidate.kind not in self.kinds
        ):
            return 0
        paths = entry.candidate.scope.paths
        path_matches = sum(
            path in query
            or any(
                path == requested
                or path.startswith(requested + "/")
                or requested.startswith(path + "/")
                for requested in self.related_paths
            )
            for path in paths
        )
        lexical = len(
            _words(query) & _words(entry.candidate.statement + " " + " ".join(paths))
        )
        return 8 * path_matches + 2 * lexical + bool(self.kinds)

    async def retrieve(self, query: str) -> MemoryContext:
        try:
            async with asyncio.timeout(self.limits.seconds):
                return await self._retrieve(query[:4000])
        except Exception:
            # Fixed diagnostics only. CancelledError is a BaseException and propagates.
            return MemoryContext(read_error=True, max_tokens=self.limits.tokens)

    async def _retrieve(self, query: str) -> MemoryContext:
        await asyncio.sleep(0)
        ranked = [(self._score(entry, query), entry) for entry in self.store.entries()]
        ranked = sorted(
            (pair for pair in ranked if pair[0]),
            key=lambda pair: (-pair[0], pair[1].id),
        )[: self.limits.candidates]
        checker = FreshnessChecker(self.resolver)
        counts: Counter[Freshness] = Counter()
        items: tuple[MemoryContextItem, ...] = ()
        dropped = 0
        for _, entry in ranked:
            await asyncio.sleep(0)
            result = await checker.check(entry)
            counts[result.status] += 1
            if result.status is not Freshness.FRESH or result.attestation_index is None:
                continue
            attestation = entry.attestations[result.attestation_index]
            item = MemoryContextItem(
                id=entry.id,
                statement=entry.candidate.statement,
                kind=entry.candidate.kind,
                scope=entry.candidate.scope,
                evidence_tree=attestation.capsule.evidence_oid,
                source_task_id=attestation.capsule.source_task_id,
                attested_at=attestation.approved_at,
            )
            proposed = (*items, item)
            if len(render_memory(proposed).encode()) > self.limits.bytes:
                dropped += 1
                continue
            items = proposed
            if len(items) == self.limits.items:
                break
        return MemoryContext(
            items=items,
            max_tokens=self.limits.tokens,
            checked_count=sum(counts.values()),
            freshness_counts=tuple(sorted(counts.items())),
            dropped_for_size=dropped,
        )
