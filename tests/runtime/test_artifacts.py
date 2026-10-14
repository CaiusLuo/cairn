"""What ``atomic_write_text`` guarantees to every durable-artifact owner."""

import os
from os import PathLike
from pathlib import Path

import pytest

from cairn.runtime.artifacts import atomic_write_text

StrOrBytesPath = str | bytes | PathLike[str] | PathLike[bytes]


def test_write_replaces_the_previous_file_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    destination.write_text("previous\n")

    atomic_write_text(destination, "next\n")

    assert destination.read_text() == "next\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_readers_never_observe_a_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement must be a rename, not an in-place truncation."""
    destination = tmp_path / "report.json"
    atomic_write_text(destination, "first\n")
    seen: list[str] = []

    real_replace = os.replace

    def observe_then_replace(source: StrOrBytesPath, target: StrOrBytesPath) -> None:
        # The destination still holds the complete previous file at this instant.
        seen.append(destination.read_text())
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", observe_then_replace)

    atomic_write_text(destination, "second\n")

    assert seen == ["first\n"]
    assert destination.read_text() == "second\n"


def test_cleanup_failure_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed temporary-file cleanup must never replace the real cause.

    The report owner annotates the error it receives with recovery coordinates.
    If cleanup could overwrite that error, the recovery note would describe the
    wrong failure.
    """
    destination = tmp_path / "report.json"

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("original rename cause")

    def fail_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(OSError) as error:
        atomic_write_text(destination, "payload\n")

    assert "original rename cause" in str(error.value)
    assert "cleanup failed" not in str(error.value)
