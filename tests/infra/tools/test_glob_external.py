"""Searching outside the Workspace, and credential files.

A caller may name any readable directory as the search root. Credential material
is reported as blocked rather than returned, so the model learns it exists and
can ask the operator instead of seeing nothing.
"""

import asyncio
from pathlib import Path

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments, PermissionRefused
from cairn.infra.tools.glob import GlobTool
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inside.py").write_text("x", encoding="utf-8")
    return Workspace(root)


def _search(workspace: Workspace, **arguments: object) -> ToolResult:
    return asyncio.run(GlobTool(workspace).execute(dict(arguments)))


def test_the_default_root_is_still_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = _search(workspace, pattern="**/*.py")
    assert result.stdout == "inside.py"
    assert result.blocked_paths == ()
    assert result.error_type is None


def test_a_named_directory_is_searched(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.py").write_text("y", encoding="utf-8")

    result = _search(workspace, pattern="**/*.py", path=str(outside))

    assert result.stdout == "elsewhere.py"
    assert result.error_type is None


def test_credential_files_are_blocked_not_returned(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    (outside / ".ssh").mkdir(parents=True)
    (outside / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")
    (outside / ".env").write_text("TOKEN=1", encoding="utf-8")
    (outside / "server.pem").write_text("key", encoding="utf-8")
    (outside / "ordinary.py").write_text("ok", encoding="utf-8")

    result = _search(workspace, pattern="**/*", path=str(outside))

    # The ordinary file is returned; the credential files are named as blocked.
    assert "ordinary.py" in result.stdout
    assert result.blocked_paths == (".env", ".ssh/id_rsa", "server.pem")
    assert ".env" not in result.stdout.split("Blocked:")[0]
    assert result.error_type == "permission_required"


def test_an_ordinary_dotfile_is_not_a_credential(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".gitignore").write_text("ok", encoding="utf-8")
    (outside / ".prettierrc").write_text("{}", encoding="utf-8")

    result = _search(workspace, pattern="**/.*", path=str(outside))

    assert result.blocked_paths == ()
    assert ".gitignore" in result.stdout


@pytest.mark.parametrize("pattern", ["/etc/*", "../*", ".git/*"])
def test_the_pattern_stays_relative_to_the_root(tmp_path: Path, pattern: str) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(InvalidArguments):
        _search(workspace, pattern=pattern)


def test_a_non_directory_root_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    with pytest.raises(InvalidArguments, match="directory"):
        _search(workspace, pattern="*", path=str(a_file))


def test_a_missing_grant_reports_structured_permission(tmp_path: Path) -> None:
    """A write outside needs approval; the error names the blocked target."""
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "out.txt"

    with pytest.raises(PermissionRefused) as refusal:
        asyncio.run(WriteTool(workspace).execute({"path": str(target), "content": "x"}))

    assert refusal.value.blocked_paths == (str(target.resolve()),)
