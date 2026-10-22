"""Bash tool contract for the filesystem-access capability (no sandbox needed)."""

from pathlib import Path

import pytest

from cairn.core.permissions import PermissionCapability
from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace


def _tool(tmp_path: Path) -> BashTool:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return BashTool(Workspace(workspace))


def test_extra_roots_require_justification(tmp_path: Path) -> None:
    with pytest.raises(InvalidArguments, match="justification"):
        _tool(tmp_path).validate(
            {"command": "true", "extra_writable_roots": [str(tmp_path)]}
        )


def test_extra_roots_rejects_network_in_one_call(tmp_path: Path) -> None:
    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {
                "command": "true",
                "extra_writable_roots": [str(tmp_path)],
                "network_access": True,
                "justification": "both",
            }
        )


def test_extra_roots_must_be_non_empty_strings(tmp_path: Path) -> None:
    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate(
            {
                "command": "true",
                "extra_writable_roots": [""],
                "justification": "j",
            }
        )


def test_unknown_argument_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidArguments):
        _tool(tmp_path).validate({"command": "true", "extra_writable_root": ["x"]})


def test_requested_capability_reports_filesystem(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    request = tool.requested_capability(
        {
            "command": "true",
            "extra_writable_roots": [str(tmp_path)],
            "justification": "j",
        }
    )
    assert request is not None
    assert request.capability is PermissionCapability.FILESYSTEM_ACCESS
    assert request.paths == (str(tmp_path),)


def test_requested_capability_none_for_plain_call(tmp_path: Path) -> None:
    assert _tool(tmp_path).requested_capability({"command": "git status"}) is None
