"""Searching outside the Workspace, and the material grep must not return.

A caller may name any readable directory as the search root, because reading
outside the Workspace needs no grant. Credential files and Git metadata are
never returned as content, so the capability cannot be used to read them.
"""

import asyncio
from pathlib import Path

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.grep import GrepTool
from cairn.infra.workspace.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inside.py").write_text("INSIDE_MARKER = 1\n", encoding="utf-8")
    return Workspace(root)


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "normal.py").write_text("OUTSIDE_MARKER = 1\n", encoding="utf-8")
    return outside


def _search(workspace: Workspace, **arguments: object) -> ToolResult:
    return asyncio.run(GrepTool(workspace).execute(dict(arguments)))


def test_an_outside_directory_is_searched(tmp_path: Path) -> None:
    """A named outside directory is a legal root; it must not crash."""
    workspace = _workspace(tmp_path)
    outside = _outside(tmp_path)

    result = _search(workspace, pattern="OUTSIDE_MARKER", path=str(outside))

    assert "OUTSIDE_MARKER" in result.stdout
    assert result.error_type is None


def test_an_outside_file_is_searched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = _outside(tmp_path)

    result = _search(
        workspace, pattern="OUTSIDE_MARKER", path=str(outside / "normal.py")
    )

    assert "OUTSIDE_MARKER" in result.stdout


def test_an_outside_result_is_named_without_the_workspace_prefix(
    tmp_path: Path,
) -> None:
    """An outside hit cannot be Workspace-relative, so it is named absolutely."""
    workspace = _workspace(tmp_path)
    outside = _outside(tmp_path)

    result = _search(workspace, pattern="OUTSIDE_MARKER", path=str(outside))

    assert "1: OUTSIDE_MARKER = 1" in result.stdout
    assert "normal.py" in result.stdout


def test_outside_git_metadata_is_not_returned(tmp_path: Path) -> None:
    """`.git` contents are never a search result, from any root."""
    workspace = _workspace(tmp_path)
    outside = _outside(tmp_path)
    (outside / ".git").mkdir()
    (outside / ".git" / "config").write_text("GITCONFIG_MARKER\n", encoding="utf-8")

    result = _search(workspace, pattern="GITCONFIG", path=str(outside))

    assert "GITCONFIG_MARKER" not in result.stdout
    assert ".git" not in result.stdout


def test_outside_credential_content_is_withheld(tmp_path: Path) -> None:
    """A credential file outside the Workspace is named, never read."""
    workspace = _workspace(tmp_path)
    outside = _outside(tmp_path)
    (outside / ".env").write_text("AWS_SECRET=SECRET_VALUE\n", encoding="utf-8")

    result = _search(workspace, pattern="AWS_SECRET", path=str(outside))

    # The value never appears; the path is named so the model can ask.
    assert "SECRET_VALUE" not in result.stdout
    assert any(path.endswith(".env") for path in result.blocked_paths)
    assert result.error_type == "permission_required"


def test_in_workspace_credential_content_is_withheld(tmp_path: Path) -> None:
    """The same rule applies inside the Workspace, not only outside it."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".env").write_text("TOKEN=INSIDE_VALUE\n", encoding="utf-8")
    (root / "ordinary.py").write_text("ORDINARY = 1\n", encoding="utf-8")

    result = asyncio.run(GrepTool(Workspace(root)).execute({"pattern": "TOKEN"}))

    assert "INSIDE_VALUE" not in result.stdout
    assert result.blocked_paths == (".env",)
    assert result.error_type == "permission_required"


def test_a_missing_outside_path_reports_what_was_asked_for(tmp_path: Path) -> None:
    """A missing root stays a described failure, not an empty result."""
    workspace = _workspace(tmp_path)
    missing = tmp_path / "missing"
    missing.mkdir()

    with pytest.raises(FileNotFoundError, match=r"nope\.txt"):
        _search(workspace, pattern="x", path=str(missing / "nope.txt"))


def test_a_pattern_that_escapes_is_still_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(InvalidArguments):
        _search(workspace, pattern="x", path="../outside")
