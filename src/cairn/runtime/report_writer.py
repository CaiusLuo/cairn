"""What the retained-work report writer guarantees to its owners.

Workflow, review and publication each keep one recovery report beside a retained
worktree. Every one of them must persist that report atomically and, when the
write fails, raise its own persistence error carrying the coordinates a human
needs to find the retained work. That protocol is what this module owns.

The report type, the error type and the failure message stay with the owner:
those differ per operation and are not part of the shared behaviour.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.infra.git.worktree import WorktreeHandle


def _no_extra_notes() -> tuple[str, ...]:
    return ()


@dataclass(slots=True)
class ReportWriter:
    """Own one retained-work report: update it, persist it, or explain the failure.

    ``handle`` supplies the retained worktree coordinates, ``persist`` performs the
    write, and ``error_factory`` builds the owner's persistence error so callers
    still catch the type they document. ``extra_notes`` adds the coordinates only
    some owners have, such as a branch or base revision.
    """

    report: Any
    recovery_path: Path
    handle: WorktreeHandle
    persist: Callable[[Any, Path], None]
    error_factory: Callable[[], Exception]
    extra_notes: Callable[[], Sequence[str]] = _no_extra_notes

    def coordinates(self) -> tuple[str, ...]:
        """Where the retained work lives, for any error that abandons it."""
        return (
            f"Retained worktree: {self.handle.path}",
            f"Recovery report: {self.recovery_path}",
        )

    def annotate(self, error: BaseException) -> None:
        """Attach the retained-work coordinates to an error about to be raised."""
        for note in (*self.coordinates(), *self.extra_notes()):
            error.add_note(note)

    def save(self, **updates: Any) -> None:
        """Persist the updated report, or raise the owner's annotated error.

        An owner that rebuilds its report from live state passes no updates; the
        copy is then skipped rather than cloning the report unchanged.
        """
        if updates:
            self.report = self.report.model_copy(update=updates)
        try:
            self.persist(self.report, self.recovery_path)
        except Exception:
            failure = self.error_factory()
            self.annotate(failure)
            raise failure from None
