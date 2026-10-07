"""Trace storage: one JSONL file per trace, queried with bounded reads.

The JSONL file is the only persistent record of a trace. Listing metadata is
read from the tail of each file (the final root span), so no summary sidecar has
to be written or kept in sync. Legacy ``summaries/`` entries are derived data
and are removed as they are encountered.
"""

import heapq
import json
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairn.observability.models import (
    Span,
    SpanStatus,
    TraceListResult,
    TraceSummary,
)
from cairn.observability.resolver import TraceResolver

#: Bounded tail window used to find a trace's final root record.
TAIL_WINDOW_BYTES = 64 * 1024
DEFAULT_LIST_LIMIT = 20


@dataclass(frozen=True, slots=True)
class TraceDeleteResult:
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class TraceStore:
    """Owns the trace directory: source-of-truth files and their queries."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.resolver = TraceResolver(root)

    def read(self, trace_id: str) -> list[Span]:
        return self._read_detail(self.resolver.resolve(trace_id))

    def count(self) -> int:
        return sum(1 for _ in self.root.glob("*.jsonl"))

    def list_traces(self, limit: int = DEFAULT_LIST_LIMIT) -> TraceListResult:
        result = TraceListResult()
        readable: set[str] = set()

        for path in self._detail_paths():
            summary, diagnostic = self._summarize(path)
            if summary is None:
                result.diagnostics.append(diagnostic or "")
                continue
            readable.add(path.stem)
            result.traces.append(summary)

        self._discard_legacy_summaries(readable)
        if type(limit) is int and limit > 0 and limit * 10 < len(result.traces):
            # Select only a small requested prefix instead of sorting every trace.
            result.traces = heapq.nlargest(
                limit,
                result.traces,
                key=lambda item: item.start_time.timestamp(),
            )
        else:
            result.traces.sort(
                key=lambda item: item.start_time.timestamp(), reverse=True
            )
            result.traces = result.traces[:limit]
        return result

    def delete(self, trace_id: str) -> TraceDeleteResult:
        resolved = self.resolver.resolve(trace_id)
        self._remove(resolved)
        return TraceDeleteResult(deleted=[resolved])

    def delete_oldest(self, count: int) -> TraceDeleteResult:
        """Delete the ``count`` oldest traces, i.e. the tail of ``list_traces``.

        Only traces whose final root record can be read are eligible. Anything
        unreadable is reported and left alone, so one bad file cannot fail the
        whole batch.
        """
        if type(count) is not int or count < 1:
            raise ValueError("count must be a positive integer")

        result = TraceDeleteResult()
        dated: list[tuple[float, str]] = []

        for path in self._detail_paths():
            summary, diagnostic = self._summarize(path)
            if summary is None:
                result.skipped.append(diagnostic or "")
            else:
                dated.append((summary.start_time.timestamp(), path.stem))

        if count * 10 < len(dated):
            oldest = heapq.nsmallest(count, dated)
        else:
            dated.sort()
            oldest = dated[:count]
        for _, stem in oldest:
            try:
                self._remove(stem)
            except OSError as exc:
                result.skipped.append(
                    f"could not delete trace {stem[:8]} ({type(exc).__name__})"
                )
            else:
                result.deleted.append(stem)

        return result

    def _detail_paths(self) -> list[Path]:
        return sorted(self.root.glob("*.jsonl"))

    def _read_detail(self, stem: str) -> list[Span]:
        path = self.root / f"{stem}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Trace not found: {stem}")

        spans: list[Span] = []
        try:
            with path.open(encoding="utf-8") as trace_file:
                for raw_line in trace_file:
                    for line in raw_line.splitlines():
                        if line.strip():
                            spans.append(Span.model_validate_json(line))
        except ValueError:
            spans.clear()
        else:
            return spans

        # Preserve eager UTF-8 decoding before validation without chaining the
        # streaming failure into any exception raised by the fallback.
        return [
            Span.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _summarize(self, path: Path) -> tuple[TraceSummary | None, str | None]:
        """Derive listing metadata from the trace's final root record."""
        short_id = path.stem[:8]
        try:
            record, saw_record = self._root_record(path)
            if record is None:
                if saw_record or path.stat().st_size == 0:
                    return None, f"skipped incomplete trace {short_id}"
                return None, f"skipped corrupt trace {short_id}"
            span = Span.model_validate(record)
            if span.end_time is None or span.status not in (
                SpanStatus.OK,
                SpanStatus.ERROR,
            ):
                return None, f"skipped incomplete trace {short_id}"
            summary = TraceSummary.from_root(span)
            if summary.trace_id != path.stem:
                raise ValueError("Root trace ID does not match its filename")
        except (OSError, ValueError) as exc:
            return None, f"skipped corrupt trace {short_id} ({type(exc).__name__})"
        return summary, None

    @staticmethod
    def _root_record(path: Path) -> tuple[dict[str, Any] | None, bool]:
        """Read the last completed root record, without parsing the whole file.

        Returns the record (if any) and whether the file holds any parseable
        record at all, so a truncated file can be told apart from an unfinished
        trace.
        """
        size = path.stat().st_size
        window = min(size, TAIL_WINDOW_BYTES)
        saw_record = False

        while True:
            for raw in reversed(_tail_bytes(path, size, window).splitlines()):
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                saw_record = True
                context = record.get("context")
                if isinstance(context, dict) and context.get("parent_span_id") is None:
                    return record, saw_record

            if window >= size:
                return None, saw_record
            # A root record far from the tail is unusual; widen once to be sure.
            window = size

    def _remove(self, stem: str) -> None:
        # Derived cache first: an interrupted delete then leaves the source of
        # truth, never a summary pointing at a trace that is already gone.
        with suppress(OSError):
            (self.root / "summaries" / f"{stem}.json").unlink()
        (self.root / f"{stem}.jsonl").unlink()

    def _discard_legacy_summaries(self, readable: set[str]) -> None:
        """Remove legacy sidecars for readable traces and missing trace files."""
        summary_root = self.root / "summaries"
        for path in summary_root.glob("*.json"):
            if path.stem in readable or not (self.root / f"{path.stem}.jsonl").exists():
                with suppress(OSError):
                    path.unlink()
        with suppress(OSError):
            summary_root.rmdir()


def _tail_bytes(path: Path, size: int, window: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(max(0, size - window))
        blob = handle.read()

    if size > window:
        # The window may start mid-line; drop that partial first line.
        _, separator, remainder = blob.partition(b"\n")
        return remainder if separator else b""
    return blob
