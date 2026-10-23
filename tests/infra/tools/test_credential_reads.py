"""Credential content must not be readable through a file tool.

``is_credential_path`` already decided which paths hold credential material, but
only ``glob`` consulted it, and ``glob`` returns names rather than bytes. These
tests pin the rule where it matters: a tool that would return content refuses and
names the target, so the model can ask for approval instead of reading it.

The refusal is raised from ``execute``, not from ``validate``: preflight answers
"is this a well-formed target", and authority is decided after it.
"""

import asyncio
from pathlib import Path

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools.base import PermissionRefused
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.patch import PatchTool
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace

CREDENTIAL_LAYOUT = (
    (".ssh/id_rsa", "PRIVATE_KEY_BYTES\n"),
    (".env", "ENV_BYTES=1\n"),
    ("server.pem", "PEM_BYTES\n"),
    ("aws/credentials", "CREDENTIALS_BYTES\n"),
)


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def _materialise(root: Path) -> None:
    for relative, content in CREDENTIAL_LAYOUT:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _read(workspace: Workspace, path: str) -> ToolResult:
    return asyncio.run(ReadFileTool(workspace).execute({"path": path}))


@pytest.mark.parametrize(("relative", "content"), CREDENTIAL_LAYOUT)
def test_read_file_refuses_credential_content_in_the_workspace(
    tmp_path: Path, relative: str, content: str
) -> None:
    workspace = _workspace(tmp_path)
    _materialise(workspace.root)

    with pytest.raises(PermissionRefused) as refusal:
        _read(workspace, relative)

    # The refusal names the target and carries no bytes.
    assert content.strip() not in str(refusal.value)
    assert refusal.value.blocked_paths


@pytest.mark.parametrize(("relative", "content"), CREDENTIAL_LAYOUT)
def test_read_file_refuses_credential_content_outside_the_workspace(
    tmp_path: Path, relative: str, content: str
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _materialise(outside)

    with pytest.raises(PermissionRefused) as refusal:
        _read(workspace, str(outside / relative))

    assert content.strip() not in str(refusal.value)
    assert refusal.value.blocked_paths


def test_an_ordinary_file_is_still_readable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.root / "ordinary.py").write_text("ORDINARY = 1\n", encoding="utf-8")

    result = _read(workspace, "ordinary.py")

    assert result.stdout == "ORDINARY = 1\n"


def test_an_ordinary_dotfile_is_still_readable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

    result = _read(workspace, ".gitignore")

    assert result.stdout == "*.pyc\n"


def test_edit_file_refuses_credential_content(tmp_path: Path) -> None:
    """edit_file reads the file to find old_text, so it must refuse too."""
    workspace = _workspace(tmp_path)
    _materialise(workspace.root)

    with pytest.raises(PermissionRefused):
        asyncio.run(
            EditFileTool(workspace).execute(
                {"path": ".env", "old_text": "ENV_BYTES=1\n", "new_text": "x\n"}
            )
        )

    assert (workspace.root / ".env").read_text(encoding="utf-8") == "ENV_BYTES=1\n"


def test_patch_file_refuses_credential_content(tmp_path: Path) -> None:
    """patch_file reads the whole file, so it must refuse too."""
    workspace = _workspace(tmp_path)
    _materialise(workspace.root)

    with pytest.raises(PermissionRefused):
        asyncio.run(
            PatchTool(workspace).execute(
                {
                    "path": "server.pem",
                    "patches": [{"old_text": "PEM_BYTES\n", "new_text": "x\n"}],
                }
            )
        )

    assert (workspace.root / "server.pem").read_text(encoding="utf-8") == "PEM_BYTES\n"


def test_expected_content_cannot_confirm_a_credential(tmp_path: Path) -> None:
    """The guard must not become a pre-approval oracle for credential bytes."""
    workspace = _workspace(tmp_path)
    _materialise(workspace.root)
    tool = WriteTool(workspace)

    with pytest.raises(PermissionRefused):
        asyncio.run(
            tool.execute(
                {
                    "path": ".env",
                    "content": "replaced\n",
                    "expected_content": "ENV_BYTES=1\n",
                }
            )
        )


def test_a_mismatched_expected_content_outside_the_workspace_is_a_tool_error(
    tmp_path: Path,
) -> None:
    """A wrong guess is a described mismatch, not an internal crash."""
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("actual\n", encoding="utf-8")

    with pytest.raises(Exception) as failure:
        asyncio.run(
            WriteTool(workspace).execute(
                {"path": str(target), "content": "x", "expected_content": "guess\n"}
            )
        )

    assert "not in the subpath of" not in str(failure.value)
