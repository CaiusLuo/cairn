"""Workspace trust and outside-workspace access for the file tools.

Cairn is local-first, so the file tools are the boundary that matters:

* the started directory is trusted once, before the first turn;
* a path outside it is readable;
* writing outside it needs a grant covering that directory, remembered for the
  rest of the session.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from cairn.core.permissions import (
    AccessKind,
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace


def _handler(
    workspace: Path,
    prompt: Callable[[PermissionRequest], PermissionChoice] | None = None,
) -> SessionPermissionHandler:
    return SessionPermissionHandler(
        prompt,
        workspace_root=workspace,
        root_normalizer=normalize_access_root,
    )


def _trusted(workspace: Path) -> SessionPermissionHandler:
    """A handler that already accepted the workspace trust prompt."""
    handler = _handler(workspace)
    handler.grants.add(PermissionCapability.WORKSPACE_TRUST)
    return handler


def test_workspace_trust_is_asked_once_and_remembered(tmp_path: Path) -> None:
    prompts: list[object] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = _handler(tmp_path, prompt)
    first = handler.ensure_workspace_trust()
    second = handler.ensure_workspace_trust()

    assert first.allowed and first.prompted
    assert first.granted_capabilities == frozenset(
        {PermissionCapability.WORKSPACE_TRUST}
    )
    assert second.allowed and not second.prompted
    assert len(prompts) == 1


def test_workspace_trust_denied_leaves_nothing_granted(tmp_path: Path) -> None:
    handler = _handler(tmp_path, lambda request: PermissionChoice.DENY)

    result = handler.ensure_workspace_trust()

    assert not result.allowed
    assert handler.grants == set()


def test_workspace_trust_without_a_prompt_fails_closed(tmp_path: Path) -> None:
    handler = _handler(tmp_path)

    result = handler.ensure_workspace_trust()

    assert not result.allowed
    assert result.source.value == "no_handler"


def test_inside_the_workspace_needs_trust_but_no_path_grant(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")

    trusted = _trusted(tmp_path)
    decision = trusted.access_decision(
        outside=False, access=AccessKind.WRITE, path=target
    )
    assert decision.allowed

    untrusted = _handler(tmp_path, lambda request: PermissionChoice.DENY)
    refused = untrusted.access_decision(
        outside=False, access=AccessKind.WRITE, path=target
    )
    assert not refused.allowed


def test_reading_outside_the_workspace_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("readable", encoding="utf-8")

    handler = _trusted(workspace)
    decision = handler.access_decision(
        outside=True, access=AccessKind.READ, path=outside
    )

    assert decision.allowed
    assert decision.granted_capabilities == frozenset()

    result = asyncio.run(
        ReadFileTool(Workspace(workspace)).execute({"path": str(outside)})
    )
    assert result.stdout == "readable"


def test_writing_outside_the_workspace_requires_a_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()

    handler = _trusted(workspace)
    assert not handler.access_decision(
        outside=True, access=AccessKind.WRITE, path=outside / "a.txt"
    ).allowed

    with pytest.raises(InvalidArguments, match="requires approval"):
        asyncio.run(
            WriteTool(Workspace(workspace)).execute(
                {"path": str(outside / "a.txt"), "content": "x"}
            )
        )


def test_an_approved_directory_covers_later_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()
    prompts: list[object] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = _handler(workspace, prompt)
    handler.grants.add(PermissionCapability.WORKSPACE_TRUST)

    first = handler.access_decision(
        outside=True, access=AccessKind.WRITE, path=outside / "a.txt"
    )
    second = handler.access_decision(
        outside=True, access=AccessKind.WRITE, path=outside / "b.txt"
    )

    assert first.allowed and first.prompted
    assert second.allowed and not second.prompted
    assert second.source.value == "session_grant"
    assert len(prompts) == 1


def test_an_approved_grant_lets_the_tool_write(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()
    target = outside / "written.txt"

    result = asyncio.run(
        WriteTool(Workspace(workspace)).execute(
            {"path": str(target), "content": "granted"},
            context=_context((outside.resolve(),)),
        )
    )

    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == "granted"


def test_git_metadata_outside_the_workspace_stays_unreachable(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    metadata = tmp_path / "repo" / ".git" / "config"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("keep", encoding="utf-8")

    with pytest.raises(InvalidArguments):
        asyncio.run(ReadFileTool(Workspace(workspace)).execute({"path": str(metadata)}))


def test_edit_outside_needs_a_grant_and_works_once_given(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("old", encoding="utf-8")

    tool = EditFileTool(Workspace(workspace))
    with pytest.raises(InvalidArguments, match="requires approval"):
        asyncio.run(
            tool.execute({"path": str(target), "old_text": "old", "new_text": "new"})
        )

    asyncio.run(
        tool.execute(
            {"path": str(target), "old_text": "old", "new_text": "new"},
            context=_context((outside.resolve(),)),
        )
    )
    assert target.read_text(encoding="utf-8") == "new"


def _context(roots: tuple[Path, ...]) -> ToolExecutionContext:
    return ToolExecutionContext(accessible_roots=roots)
