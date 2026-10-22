"""External filesystem access belongs to the file tools, not the shell.

A local shell runs as the user, so it needs no per-directory grant and this
module no longer asserts anything about mounts. What still holds is the file-tool
boundary: a path outside the Workspace is refused unless the session approved a
``FILESYSTEM_ACCESS`` grant covering it.
"""

from pathlib import Path

import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
)
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.workspace.paths import resolve_workspace_path


def _tool_call(path: Path) -> ToolCall:
    return ToolCall(id="fs", name="read_file", arguments={"path": str(path)})


def _handler(prompt) -> SessionPermissionHandler:
    return SessionPermissionHandler(prompt, root_normalizer=normalize_access_root)


def _request(path: Path) -> PermissionRequest:
    return PermissionRequest(
        capability=PermissionCapability.FILESYSTEM_ACCESS,
        justification="read a data file",
        tool_call=_tool_call(path),
        paths=(str(path),),
    )


def test_workspace_path_rule_still_refuses_an_outside_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()

    with pytest.raises(ValueError):
        resolve_workspace_path(workspace, str(outside))

    assert (
        resolve_workspace_path(workspace, "src/main.py")
        == (workspace / "src/main.py").resolve()
    )


def test_grant_is_required_before_an_outside_path_is_usable(tmp_path: Path) -> None:
    outside = tmp_path / "data"
    outside.mkdir()
    handler = _handler(lambda request: PermissionChoice.DENY)

    result = handler.authorize(_request(outside))

    assert not result.allowed
    assert result.source is PermissionSource.USER_DENIED
    assert handler.granted_roots == set()


def test_session_grant_is_reused_for_a_covered_path(tmp_path: Path) -> None:
    outside = tmp_path / "data"
    (outside / "child").mkdir(parents=True)
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = _handler(prompt)
    first = handler.authorize(_request(outside))
    second = handler.authorize(_request(outside / "child"))

    assert first.allowed and first.prompted
    assert second.allowed and not second.prompted
    assert second.source is PermissionSource.SESSION_GRANT
    assert len(prompts) == 1


def test_grant_cannot_cover_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    handler = SessionPermissionHandler(
        lambda request: PermissionChoice.ALLOW_SESSION,
        workspace_root=workspace,
        root_normalizer=normalize_access_root,
    )

    with pytest.raises(ValueError):
        handler.authorize(_request(tmp_path))


def test_grant_cannot_name_a_system_tree() -> None:
    handler = _handler(lambda request: PermissionChoice.ALLOW_SESSION)
    for blocked in ("/", "/etc", "/usr", "/proc"):
        with pytest.raises(ValueError):
            handler.authorize(_request(Path(blocked)))


def test_shell_needs_no_directory_grant() -> None:
    """A shell call carries no path arguments at all any more."""
    shell = ToolCall(id="s", name="bash", arguments={"command": "ls /tmp"})
    assert "extra_writable_roots" not in shell.arguments
