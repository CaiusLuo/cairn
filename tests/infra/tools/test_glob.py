import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.glob import MAX_SCAN_CANDIDATES, GlobTool
from cairn.infra.workspace.workspace import Workspace


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_glob_returns_workspace_relative_paths(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py")
    _write(tmp_path / "src" / "b.py")
    _write(tmp_path / "src" / "sub" / "c.py")
    _write(tmp_path / "README.md")
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "**/*.py"}))

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "src/a.py",
        "src/b.py",
        "src/sub/c.py",
    ]


def test_glob_matches_at_the_workspace_root(tmp_path: Path) -> None:
    _write(tmp_path / "top.py")
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "*.py"}))

    assert result.stdout.splitlines() == ["top.py"]


def test_glob_reports_no_match(tmp_path: Path) -> None:
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "**/*.py"}))

    assert result.exit_code == 0
    assert "No workspace files match" in result.stdout


def test_glob_marks_output_truncated_at_the_limit(tmp_path: Path) -> None:
    for index in range(5):
        _write(tmp_path / f"file{index}.py")
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "*.py", "limit": 3}))

    assert len(result.stdout.splitlines()) == 3
    assert result.stdout_truncated is True


def test_glob_bounds_the_scan_itself(tmp_path: Path) -> None:
    for index in range(MAX_SCAN_CANDIDATES + 20):
        _write(tmp_path / "many" / f"f{index}.py", "x")
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "many/*.py", "limit": 1}))

    assert result.stdout_truncated is True
    assert len(result.stdout.splitlines()) == 1


def test_glob_omits_directories_and_git(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    _write(tmp_path / ".git" / "b.py")
    tool = GlobTool(Workspace(tmp_path))

    result = asyncio.run(tool.execute({"pattern": "**/*.py"}))

    assert result.stdout.splitlines() == ["src/a.py"]


def test_glob_does_not_escape_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.py")
    workspace_root = tmp_path / "project"
    _write(workspace_root / "inside.py")
    (workspace_root / "link").symlink_to(outside)
    tool = GlobTool(Workspace(workspace_root))

    result = asyncio.run(tool.execute({"pattern": "**/*.py"}))

    assert result.stdout.splitlines() == ["inside.py"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"pattern": ""},
        {"pattern": "   "},
        {"pattern": "*.py", "limit": 0},
        {"pattern": "*.py", "limit": "3"},
    ],
)
def test_glob_rejects_invalid_arguments(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    tool = GlobTool(Workspace(tmp_path))

    with pytest.raises(InvalidArguments):
        tool.validate(arguments)
