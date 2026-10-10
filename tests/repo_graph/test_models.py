from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cairn.repo_graph import BuildLimits, BuildReport, RepoGraph


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_build_limits_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BuildLimits(max_entries=value)  # type: ignore[arg-type]


def test_build_limits_are_immutable() -> None:
    limits = BuildLimits()
    with pytest.raises(FrozenInstanceError):
        limits.max_entries = 1  # type: ignore[misc]


def test_snapshot_is_read_only(tmp_path: Path) -> None:
    graph = RepoGraph(tmp_path, (), (), (), (), BuildReport(True, 1, 0, (), ()))
    with pytest.raises(FrozenInstanceError):
        graph.files = ()  # type: ignore[misc]
