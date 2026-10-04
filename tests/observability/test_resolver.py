import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from cairn.observability.resolver import TraceResolver


def test_resolve_unique_match_includes_directory_named_jsonl(tmp_path: Path) -> None:
    (tmp_path / "unique-trace.jsonl").mkdir()

    assert TraceResolver(tmp_path).resolve("unique") == "unique-trace"


def test_resolve_reports_missing_trace_exactly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"^Trace not found: absent$"):
        TraceResolver(tmp_path).resolve("absent")


@pytest.mark.parametrize("trace_id", ["", "../outside", "/tmp/outside"])
def test_resolve_rejects_invalid_ids_exactly(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(
        ValueError,
        match="^Invalid trace ID: " + re.escape(trace_id) + "$",
    ):
        TraceResolver(tmp_path).resolve(trace_id)


def test_resolve_keeps_only_sorted_five_preview_stems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stems = [
        "trace-ff1",
        "trace-aa2",
        "trace-ee1",
        "trace-bb1",
        "trace-dd1",
        "trace-aa1",
        "trace-cc1",
        "trace-zz1",
    ]
    paths = [tmp_path / f"{stem}.jsonl" for stem in stems]
    for path in paths:
        if path.name == "trace-ee1.jsonl":
            path.mkdir()
        else:
            path.touch()

    original_glob = Path.glob

    def unordered_glob(path: Path, pattern: str) -> Iterator[Path]:
        if path == tmp_path and pattern == "*.jsonl":
            return iter(reversed(paths))
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", unordered_glob)

    with pytest.raises(ValueError) as exc_info:
        TraceResolver(tmp_path).resolve("trace-")

    assert str(exc_info.value) == (
        "Ambiguous trace prefix: trace- "
        "(8 matches: trace-aa, trace-aa, trace-bb, trace-cc, trace-dd, +3 more)"
    )


def test_complete_id_remains_ambiguous_when_a_longer_match_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "abcdef12.jsonl").touch()
    (tmp_path / "abcdef12-longer.jsonl").touch()

    with pytest.raises(ValueError) as exc_info:
        TraceResolver(tmp_path).resolve("abcdef12")

    assert str(exc_info.value) == (
        "Ambiguous trace prefix: abcdef12 (2 matches: abcdef12, abcdef12)"
    )
