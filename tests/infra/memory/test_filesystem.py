"""Per-pass byte budgets and fail-closed behaviour of descriptor observations."""

from pathlib import Path

import pytest

from cairn.core.memory.models import MemoryError, PathKind
from cairn.infra.memory.filesystem import FileObserver, ReadLimits

# Each file is exactly the 64 KiB per-file cap; the aggregate cap is 512 KiB.
_FILE_BYTES = 64 * 1024
_TOTAL_BYTES = 512 * 1024


def write_files(root: Path, count: int) -> tuple[str, ...]:
    paths = tuple(f"file{index}.py" for index in range(count))
    for path in paths:
        (root / path).write_bytes(b"x" * _FILE_BYTES)
    return paths


def observer(root: Path) -> FileObserver:
    return FileObserver(
        root, ReadLimits(file_bytes=_FILE_BYTES, total_bytes=_TOTAL_BYTES)
    )


def test_capture_resolves_scope_above_half_the_aggregate_budget(tmp_path: Path) -> None:
    """Six 64 KiB files need 384 KiB per pass, inside the 512 KiB pass budget.

    Both passes used to share one budget, so the comparison pass exceeded the cap
    and an unchanged, valid scope was reported as unreadable.
    """
    paths = write_files(tmp_path, 6)

    fingerprints = observer(tmp_path).capture(paths)

    assert [fingerprint.path for fingerprint in fingerprints] == list(paths)
    assert all(fingerprint.kind is PathKind.FILE for fingerprint in fingerprints)
    assert all(fingerprint.sha256 for fingerprint in fingerprints)


def test_capture_still_fails_closed_above_the_aggregate_pass_budget(
    tmp_path: Path,
) -> None:
    paths = write_files(tmp_path, 9)  # 576 KiB in a single pass.

    with pytest.raises(MemoryError, match="read limit exceeded"):
        observer(tmp_path).capture(paths)
