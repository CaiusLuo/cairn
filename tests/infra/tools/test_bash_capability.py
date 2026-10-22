"""Bash tool contract for the filesystem-access capability (no sandbox needed)."""

from pathlib import Path

import pytest

from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace


def _tool(tmp_path: Path) -> BashTool:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
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


def test_extra_roots_are_validated_at_preflight(tmp_path: Path) -> None:
    """An unusable root fails as a tool error, not as an aborted turn."""
    for bad in ("relative/path", "/", "/etc", str(tmp_path / "missing")):
        with pytest.raises(InvalidArguments):
            _tool(tmp_path).validate(
                {
                    "command": "true",
                    "extra_writable_roots": [bad],
                    "justification": "j",
                }
            )


def test_extra_roots_inside_the_workspace_are_rejected(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(InvalidArguments):
        tool.validate(
            {
                "command": "true",
                "extra_writable_roots": [str(tool.workspace.root)],
                "justification": "j",
            }
        )
