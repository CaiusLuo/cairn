import asyncio
import os
from pathlib import Path

import pytest

from cairn.tools.files import EditFileTool, ReadFileTool


def test_read_file_returns_requested_lines_and_caps_output(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    tool = ReadFileTool(tmp_path)

    result = asyncio.run(
        tool.execute({"path": "example.py", "start_line": 2, "end_line": 3})
    )

    assert result.stdout == "two\nthree\n"
    assert result.exit_code == 0

    path.write_text("x" * 13_000, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert len(result.stdout) < 13_000
    assert result.stdout.endswith("[read_file output truncated]")
    with pytest.raises(ValueError, match="1 to 200 lines"):
        asyncio.run(tool.execute({"path": "example.py", "end_line": 201}))

    path.write_text("line\n" * 201, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert result.stdout.count("line\n") == 200
    assert result.stdout.endswith("[read_file more lines available]")


def test_edit_file_replaces_once_and_leaves_failed_edits_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.py"
    path.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    tool = EditFileTool(tmp_path)

    result = asyncio.run(
        tool.execute({"path": "example.py", "old_text": "beta", "new_text": "gamma"})
    )

    assert result.stdout == "Updated example.py"
    assert path.read_text(encoding="utf-8") == "alpha\ngamma\nalpha\n"

    for old_text, count in (("alpha", 2), ("missing", 0)):
        with pytest.raises(ValueError, match=f"matched {count} times"):
            asyncio.run(
                tool.execute(
                    {"path": "example.py", "old_text": old_text, "new_text": "bad"}
                )
            )
        assert path.read_text(encoding="utf-8") == "alpha\ngamma\nalpha\n"


def test_edit_file_keeps_original_if_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "example.py"
    path.write_text("before\n", encoding="utf-8")

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        asyncio.run(
            EditFileTool(tmp_path).execute(
                {"path": "example.py", "old_text": "before", "new_text": "after"}
            )
        )

    assert path.read_text(encoding="utf-8") == "before\n"
    assert not list(tmp_path.glob(".cairn-edit-*"))


def test_edit_file_creates_without_overwriting(tmp_path: Path) -> None:
    tool = EditFileTool(tmp_path)
    arguments = {"path": "new/example.py", "old_text": "", "new_text": "print('ok')\n"}

    result = asyncio.run(tool.execute(arguments))

    path = tmp_path / "new" / "example.py"
    assert result.stdout == "Created new/example.py"
    assert path.read_text(encoding="utf-8") == "print('ok')\n"
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(tool.execute(arguments))
    assert path.read_text(encoding="utf-8") == "print('ok')\n"


def test_file_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    for path in ("../outside.txt", "link.txt"):
        with pytest.raises(ValueError):
            asyncio.run(ReadFileTool(tmp_path).execute({"path": path}))
        with pytest.raises(ValueError):
            asyncio.run(
                EditFileTool(tmp_path).execute(
                    {"path": path, "old_text": "private", "new_text": "bad"}
                )
            )
    assert outside.read_text(encoding="utf-8") == "private"
