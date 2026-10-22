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
    _bash_capability_request,
    evaluate_permission_policy,
)
from cairn.infra.tools import filesystem_access
from cairn.infra.tools.filesystem_access import normalize_access_root


def _fs_call(*roots: str, justification: str = "clean temp") -> ToolCall:
    return ToolCall(
        id="fs",
        name="bash",
        arguments={
            "command": "rm -rf /tmp/scratch",
            "extra_writable_roots": list(roots),
            "justification": justification,
        },
    )


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


def test_bash_extra_roots_request_filesystem_capability() -> None:
    request = _bash_capability_request(_fs_call("/var/tmp/scratch"))

    assert request is not None
    assert request.capability is PermissionCapability.FILESYSTEM_ACCESS
    assert request.paths == ("/var/tmp/scratch",)


def test_network_and_no_request_still_separate() -> None:
    network = ToolCall(
        id="n",
        name="bash",
        arguments={"command": "curl x", "network_access": True, "justification": "j"},
    )
    network_request = _bash_capability_request(network)
    assert network_request is not None
    assert network_request.capability is PermissionCapability.NETWORK

    plain = ToolCall(id="p", name="bash", arguments={"command": "git status"})
    assert _bash_capability_request(plain) is None


def test_policy_asks_for_extra_roots_and_allows_baseline() -> None:
    assert evaluate_permission_policy(_fs_call("/var/tmp/x")).allowed is False

    baseline = ToolCall(id="b", name="bash", arguments={"command": "git status"})
    assert evaluate_permission_policy(baseline).allowed is True


def test_normalize_rejects_system_and_unsafe_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    for blocked in ("/", "/etc", "/usr", "/proc", "/dev"):
        with pytest.raises(ValueError):
            normalize_access_root(blocked)

    with pytest.raises(ValueError):
        normalize_access_root("relative/path")

    with pytest.raises(ValueError):
        normalize_access_root(str(tmp_path / "missing"))

    with pytest.raises(ValueError):
        normalize_access_root(str(tmp_path / "file.txt"))

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
