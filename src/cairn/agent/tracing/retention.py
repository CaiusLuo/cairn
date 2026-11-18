"""Capacity and age limits for the local trace directory.

Tracing is diagnostic rather than authoritative, so retention must never turn a
completed turn into a failed one. The wrapper writes through to the real sink
first and only then sweeps; a sweep failure is swallowed, while a write failure
still propagates so the Tracer can report ``persisted: false``.
"""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cairn.agent.tracing.models import Span
    from cairn.agent.tracing.sinks import TraceSink

DEFAULT_MAX_FILES = 500
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_SWEEP_EVERY = 32


class RetentionTraceSink:
    """Bound a trace directory by file count and age, one root span at a time."""

    def __init__(
        self,
        inner: TraceSink,
        root: Path,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        sweep_every: int = DEFAULT_SWEEP_EVERY,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be at least 1")
        if max_age_days < 1:
            raise ValueError("max_age_days must be at least 1")
        if sweep_every < 1:
            raise ValueError("sweep_every must be at least 1")
        self.inner = inner
        self.root = root
        self.max_files = max_files
        self.max_age_seconds = max_age_days * 24 * 60 * 60
        self.sweep_every = sweep_every
        self._completed = 0

    def emit(self, span: Span) -> None:
        # A write failure must remain visible to Tracer as a persistence error.
        self.inner.emit(span)
        if span.context.parent_span_id is None and span.end_time is not None:
            self._completed += 1
            if self._completed % self.sweep_every == 0:
                with suppress(Exception):
                    self.sweep()

    def sweep(self) -> int:
        """Delete traces over the age or count limit; return how many were removed."""
        files = [
            path
            for path in self.root.glob("*.jsonl")
            if path.is_file() and not path.is_symlink()
        ]
        if not files:
            return 0
        now = time.time()
        dated = sorted(
            ((path.stat().st_mtime, path) for path in files),
            key=lambda item: item[0],
        )
        expired = [
            path for modified, path in dated if now - modified > self.max_age_seconds
        ]
        remaining = [
            (modified, path) for modified, path in dated if path not in expired
        ]
        surplus = max(0, len(remaining) - self.max_files)
        removed = 0
        for path in [*expired, *(path for _, path in remaining[:surplus])]:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed
