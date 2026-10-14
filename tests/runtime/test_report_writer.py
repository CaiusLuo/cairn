"""What the retained-work report writer guarantees to its owners.

Workflow, review and publication all persist a recovery report and, when that
write fails, raise their own persistence error carrying the coordinates a human
needs to find the retained worktree. That protocol is what this module owns.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from cairn.runtime.report_writer import ReportWriter


class PersistenceFailed(RuntimeError):
    """A stand-in for the owner's own persistence error type."""


class FakeReport:
    def __init__(self, **fields: object) -> None:
        self.fields = fields

    def model_copy(self, *, update: dict[str, object]) -> "FakeReport":
        return FakeReport(**{**self.fields, **update})

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeReport) and other.fields == self.fields


class FakeHandle:
    """The retained-work coordinates a real WorktreeHandle supplies."""

    def __init__(self, path: Path) -> None:
        self.path = path


def make_writer(
    tmp_path: Path,
    persist: Callable[[Any, Path], None],
    extra_notes: Callable[[], Sequence[str]] | None = None,
) -> ReportWriter:
    writer = ReportWriter(
        report=FakeReport(status="running"),
        recovery_path=tmp_path / "report.json",
        handle=FakeHandle(tmp_path / "retained-worktree"),  # type: ignore[arg-type]
        persist=persist,
        error_factory=lambda: PersistenceFailed("persist failed"),
    )
    if extra_notes is not None:
        writer.extra_notes = extra_notes
    return writer


def test_save_persists_the_updated_report(tmp_path: Path) -> None:
    written: list[tuple[object, Path]] = []

    writer = make_writer(tmp_path, lambda report, path: written.append((report, path)))

    writer.save(status="blocked")

    assert writer.report == FakeReport(status="blocked")
    assert written == [(FakeReport(status="blocked"), tmp_path / "report.json")]


def test_save_without_updates_persists_the_report_unchanged(tmp_path: Path) -> None:
    """An owner that rebuilds its report from live state passes no updates."""

    class NoCopyReport(FakeReport):
        def model_copy(self, *, update: dict[str, object]) -> "NoCopyReport":
            raise AssertionError("must not copy a report that has no updates")

    written: list[object] = []
    writer = ReportWriter(
        report=NoCopyReport(total=1),
        recovery_path=tmp_path / "report.json",
        handle=FakeHandle(tmp_path / "retained-worktree"),  # type: ignore[arg-type]
        persist=lambda report, path: written.append(report),
        error_factory=lambda: PersistenceFailed("persist failed"),
    )

    writer.save()

    assert written == [writer.report]


def test_failure_raises_the_owners_error_with_recovery_coordinates(
    tmp_path: Path,
) -> None:
    """The raised error must locate the retained work without leaking the cause."""

    def explode(report: object, path: Path) -> None:
        raise OSError("disk on fire for /secret/token")

    writer = make_writer(tmp_path, explode)

    with pytest.raises(PersistenceFailed) as error:
        writer.save(status="blocked")

    # The raw cause is suppressed: persistence errors can carry host paths and
    # credentials, so only the owner's message and coordinates are exposed.
    assert error.value.__cause__ is None
    assert "disk on fire" not in str(error.value)
    assert "secret" not in "\n".join(error.value.__notes__)
    notes = "\n".join(error.value.__notes__)
    assert str(tmp_path / "retained-worktree") in notes
    assert str(tmp_path / "report.json") in notes


def test_owner_specific_notes_are_attached_when_supplied(tmp_path: Path) -> None:
    """A workflow adds branch and base revision; a review does not."""

    def explode(report: object, path: Path) -> None:
        raise OSError("disk on fire")

    writer = make_writer(
        tmp_path,
        explode,
        extra_notes=lambda: (
            "Retained branch: codex/issue-1",
            "Base revision: abc123",
        ),
    )

    with pytest.raises(PersistenceFailed) as error:
        writer.save(status="blocked")

    notes = "\n".join(error.value.__notes__)
    assert "Retained branch: codex/issue-1" in notes
    assert "Base revision: abc123" in notes
