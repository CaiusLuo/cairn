"""Filesystem-access capability: path validation and approval semantics."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
    evaluate_permission_policy,
)
from cairn.infra.tools import filesystem_access
from cairn.infra.tools.filesystem_access import normalize_access_root


def _fs_call(*roots: str) -> ToolCall:
    """A file-tool call that reached outside the workspace.

    Bash no longer carries ``extra_writable_roots``: a local shell needs no
    per-directory grant. External filesystem access is a file-tool concern.
    """
    return ToolCall(id="fs", name="read_file", arguments={"path": roots[0]})


def _request(paths: tuple[str, ...]) -> PermissionRequest:
    return PermissionRequest(
        capability=PermissionCapability.FILESYSTEM_ACCESS,
        justification="clean temp",
        tool_call=_fs_call(*paths),
        paths=paths,
    )


def _handler(
    prompt: Callable[[PermissionRequest], PermissionChoice] | None,
) -> SessionPermissionHandler:
    return SessionPermissionHandler(prompt, root_normalizer=normalize_access_root)


def test_policy_asks_for_a_file_tool_so_its_path_is_consulted() -> None:
    """The policy cannot know whether a path is outside, so it asks.

    The runtime then asks the tool for its capability: a tool that reports none
    is an ordinary in-workspace operation and is allowed without a prompt.
    """
    result = evaluate_permission_policy(_fs_call("/var/tmp/x"))
    assert result.policy_decision.value == "ask"


def test_policy_asks_for_a_local_shell() -> None:
    shell = ToolCall(id="b", name="bash", arguments={"command": "git status"})
    assert evaluate_permission_policy(shell).allowed is False


def test_normalize_rejects_system_and_unsafe_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    for blocked in ("/", "/etc", "/usr", "/proc", "/dev"):
        with pytest.raises(ValueError):
            normalize_access_root(blocked)

    with pytest.raises(ValueError):
        normalize_access_root("relative/path")

    # A not-yet-created directory resolves to its nearest existing ancestor: a
    # grant must be approvable before the directory it names exists.
    assert normalize_access_root(str(tmp_path / "missing")) == tmp_path.resolve()

    # A grant names a directory. A path that resolves to an existing file is not
    # a directory, so it is refused rather than silently granting its parent.
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        normalize_access_root(str(a_file))

    with pytest.raises(ValueError):
        normalize_access_root(str(workspace), protected=(workspace,))

    with pytest.raises(ValueError):
        normalize_access_root(str(tmp_path), protected=(workspace,))

    with pytest.raises(ValueError):
        normalize_access_root(str(tmp_path / ".git"))


def test_normalize_rejects_a_directory_inside_a_blocked_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "system"
    (blocked / "sub").mkdir(parents=True)
    monkeypatch.setattr(
        filesystem_access, "_BLOCKED_SYSTEM_ROOTS", frozenset({blocked})
    )

    with pytest.raises(ValueError, match="protected system"):
        normalize_access_root(str(blocked / "sub"))
    # The blocked root itself is still refused.
    with pytest.raises(ValueError, match="protected system"):
        normalize_access_root(str(blocked))


def test_normalize_accepts_a_normal_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    assert normalize_access_root(str(data)) == data.resolve()


def test_handler_session_grant_reuse_and_subset(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    child = root / "child"
    child.mkdir()

    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = _handler(prompt)

    first = handler.authorize(_request((str(root),)))
    assert first.allowed and first.prompted
    assert first.source is PermissionSource.USER_SESSION
    assert first.granted_roots == frozenset({str(root.resolve())})

    # A request inside an already-granted subtree reuses the grant.
    second = handler.authorize(_request((str(child),)))
    assert second.allowed and not second.prompted
    assert second.source is PermissionSource.SESSION_GRANT
    assert len(prompts) == 1


def test_handler_allow_once_does_not_persist(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()

    handler = _handler(lambda request: PermissionChoice.ALLOW_ONCE)
    first = handler.authorize(_request((str(root),)))
    second = handler.authorize(_request((str(root),)))

    assert first.allowed and first.prompted
    assert second.prompted
    assert handler.granted_roots == set()


def test_handler_deny_grants_nothing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()

    handler = _handler(lambda request: PermissionChoice.DENY)
    result = handler.authorize(_request((str(root),)))

    assert not result.allowed
    assert result.source is PermissionSource.USER_DENIED
    assert handler.granted_roots == set()


def test_handler_rejects_a_protected_ancestor(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    handler = SessionPermissionHandler(
        lambda request: PermissionChoice.ALLOW_SESSION,
        workspace_root=workspace,
        root_normalizer=normalize_access_root,
    )
    with pytest.raises(ValueError):
        # tmp_path is an ancestor of the workspace; granting it would expose it.
        handler.authorize(_request((str(tmp_path),)))


def test_handler_invalid_choice_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    handler = _handler(Mock(return_value="allow_session"))

    with pytest.raises(ValueError, match="Invalid permission choice"):
        handler.authorize(_request((str(root),)))
    assert handler.granted_roots == set()


def test_handler_without_prompt_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    handler = _handler(None)
    result = handler.authorize(_request((str(root),)))

    assert not result.allowed
    assert result.source is PermissionSource.NO_HANDLER


def test_handler_without_normalizer_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    handler = SessionPermissionHandler(lambda request: PermissionChoice.ALLOW_ONCE)

    with pytest.raises(ValueError, match="no root validator"):
        handler.authorize(_request((str(root),)))
