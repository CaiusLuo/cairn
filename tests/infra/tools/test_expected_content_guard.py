"""Acceptance test for write_file expected-content guard (Issue #44 real task)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.write import WRITE_FILE_MAX_BYTES, WriteTool
from cairn.infra.workspace.workspace import Workspace


def _tool(root: Path) -> WriteTool:
    return WriteTool(Workspace(root))


def test_write_rejects_when_expected_content_does_not_match(tmp_path: Path) -> None:
    """A guard must refuse to overwrite a file whose content changed."""
    path = tmp_path / "a.txt"
    path.write_text("actual\n", encoding="utf-8")

    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {"path": "a.txt", "content": "new\n", "expected_content": "stale\n"}
        )

    assert path.read_text(encoding="utf-8") == "actual\n"


def test_write_allows_when_expected_content_matches(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("old\n", encoding="utf-8")

    asyncio.run(
        _tool(tmp_path).execute(
            {"path": "a.txt", "content": "new\n", "expected_content": "old\n"}
        )
    )

    assert path.read_text(encoding="utf-8") == "new\n"


def test_write_execute_refuses_when_expected_content_does_not_match(
    tmp_path: Path,
) -> None:
    """The guard also holds through execute(), not only a direct validate()."""
    path = tmp_path / "a.txt"
    path.write_text("actual\n", encoding="utf-8")

    with pytest.raises(InvalidArguments):
        asyncio.run(
            _tool(tmp_path).execute(
                {"path": "a.txt", "content": "new\n", "expected_content": "stale\n"}
            )
        )

    assert path.read_text(encoding="utf-8") == "actual\n"
    assert [item.name for item in tmp_path.iterdir()] == ["a.txt"]


def test_write_guard_preserves_the_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "a.sh"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o755)

    asyncio.run(
        _tool(tmp_path).execute(
            {"path": "a.sh", "content": "new\n", "expected_content": "old\n"}
        )
    )

    assert path.stat().st_mode & 0o777 == 0o755


def test_write_ignores_expected_content_when_the_file_is_absent(
    tmp_path: Path,
) -> None:
    """A creation stays a creation: nothing can mismatch a missing file."""
    result = asyncio.run(
        _tool(tmp_path).execute(
            {"path": "deep/new.txt", "content": "abc\n", "expected_content": "other"}
        )
    )

    assert result.stdout == "Created deep/new.txt"
    assert (tmp_path / "deep" / "new.txt").read_text(encoding="utf-8") == "abc\n"


def test_write_guard_treats_an_empty_expected_content_as_exact(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("", encoding="utf-8")

    asyncio.run(
        _tool(tmp_path).execute(
            {"path": "a.txt", "content": "new\n", "expected_content": ""}
        )
    )

    assert path.read_text(encoding="utf-8") == "new\n"

    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {"path": "a.txt", "content": "new\n", "expected_content": ""}
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "a.txt", "content": "x", "expected_content": 1},
        {"path": "a.txt", "content": "x", "expected_content": None},
    ],
)
def test_write_rejects_a_non_string_expected_content(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(arguments)


def test_write_guard_refuses_a_file_it_cannot_verify(tmp_path: Path) -> None:
    """A non-UTF-8 file cannot match, so the guard fails closed."""
    path = tmp_path / "a.bin"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {"path": "a.bin", "content": "new\n", "expected_content": "\xff\xfe"}
        )

    assert path.read_bytes() == b"\xff\xfe"


def test_write_guard_refuses_an_oversized_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"x" * (WRITE_FILE_MAX_BYTES + 1))

    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {
                "path": "a.txt",
                "content": "new\n",
                "expected_content": "x" * (WRITE_FILE_MAX_BYTES + 1),
            }
        )

    assert path.stat().st_size == WRITE_FILE_MAX_BYTES + 1


def test_write_guard_still_refuses_to_escape_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("actual\n", encoding="utf-8")

    with pytest.raises(InvalidArguments):
        _tool(root).validate(
            {
                "path": "../outside.txt",
                "content": "new\n",
                "expected_content": "actual\n",
            }
        )

    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "actual\n"
