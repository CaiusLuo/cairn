"""Shared argument-preflight coverage for every model-facing file tool.

The per-tool argument checks are owned by ``cairn.infra.tools.arguments``, so the
security cases that must hold for all of them are covered once here and
parametrized over the real tools: read_file, edit_file, write_file, patch_file,
glob and grep. Each tool's own module keeps its tool-specific policy coverage
(line ranges, patch shape, content size, pattern syntax).

Every tool case asserts the rejection through both entry points the runtime uses:
``validate`` (the preflight that precedes permission checks) and ``execute``
(which must perform no effect for rejected arguments).
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.tools.arguments import (
    function_schema,
    reject_unknown_keys,
    workspace_path,
)
from cairn.infra.tools.base import InvalidArguments, PermissionRefused
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.glob import GlobTool
from cairn.infra.tools.grep import GrepTool
from cairn.infra.tools.patch import PatchTool
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace


@dataclass(frozen=True)
class ToolSpec:
    """One real tool plus a valid baseline argument dictionary for it."""

    name: str
    build: Callable[[Workspace], Any]
    arguments: Callable[[], dict[str, Any]]


TOOLS = (
    ToolSpec("read_file", ReadFileTool, lambda: {"path": "existing.txt"}),
    ToolSpec(
        "edit_file",
        EditFileTool,
        lambda: {"path": "existing.txt", "old_text": "old", "new_text": "new"},
    ),
    ToolSpec(
        "write_file",
        WriteTool,
        lambda: {"path": "existing.txt", "content": "new"},
    ),
    ToolSpec(
        "patch_file",
        PatchTool,
        lambda: {
            "path": "existing.txt",
            "patches": [{"old_text": "old", "new_text": "new"}],
        },
    ),
    ToolSpec("glob", GlobTool, lambda: {"pattern": "*.py"}),
    ToolSpec("grep", GrepTool, lambda: {"pattern": "needle"}),
)

# Tools whose contract includes a `path` argument. grep is one of them even
# though its baseline arguments omit the optional path.
PATH_TOOLS = tuple(
    spec
    for spec in TOOLS
    if spec.name in ("read_file", "edit_file", "write_file", "patch_file", "grep")
)
PATTERN_TOOLS = tuple(spec for spec in TOOLS if spec.name in ("glob", "grep"))


def _ids(specs: tuple[ToolSpec, ...]) -> list[str]:
    return [spec.name for spec in specs]


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A workspace beside an outside file plus file, directory and nested links."""
    root = tmp_path / "root"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("keep", encoding="utf-8")
    (root / "existing.txt").write_text("old\n", encoding="utf-8")
    (root / "real_dir").mkdir()
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    (root / "alias1").symlink_to(root / "real_dir", target_is_directory=True)
    (root / "real_dir" / "alias2").symlink_to(root / ".git", target_is_directory=True)
    return root


def _rejected(
    spec: ToolSpec,
    root: Path,
    arguments: dict[str, Any],
    *,
    match: str | None = None,
) -> None:
    """Both preflight and execution must reject the arguments.

    ``match`` additionally pins the rejection reason where the security case
    depends on it (for example, that a path failed because of a symlink rather
    than incidentally for another reason).
    """
    tool = spec.build(Workspace(root))
    with pytest.raises(InvalidArguments, match=match):
        tool.validate(dict(arguments))
    with pytest.raises(InvalidArguments, match=match):
        asyncio.run(tool.execute(dict(arguments)))


# --- the shared module itself ---------------------------------------------


def test_function_schema_wraps_parameters_in_the_function_envelope() -> None:
    parameters = {"type": "object", "properties": {}, "required": []}

    assert function_schema("demo", "A demo tool.", parameters) == {
        "type": "function",
        "function": {
            "name": "demo",
            "description": "A demo tool.",
            "parameters": parameters,
        },
    }


def test_reject_unknown_keys_accepts_only_declared_keys() -> None:
    reject_unknown_keys({"path": "a", "limit": 1}, {"path", "limit"}, tool_label="x")

    with pytest.raises(InvalidArguments):
        reject_unknown_keys({"path": "a", "other": 1}, {"path"}, tool_label="x")


def test_workspace_path_returns_the_resolved_workspace_target(
    workspace_root: Path,
) -> None:
    workspace = Workspace(workspace_root)

    resolved = workspace_path(workspace, {"path": "existing.txt"})

    assert resolved == workspace.root / "existing.txt"


@pytest.mark.parametrize(
    "raw_path",
    ["a\0b.txt", "../outside.txt", "", 1, None, "link.txt", ".git/config"],
)
def test_workspace_path_rejects_untrusted_paths(
    workspace_root: Path, raw_path: object
) -> None:
    with pytest.raises(InvalidArguments):
        workspace_path(Workspace(workspace_root), {"path": raw_path})


# --- cases that must hold for every tool ----------------------------------


@pytest.mark.parametrize("spec", TOOLS, ids=_ids(TOOLS))
def test_undeclared_argument_is_rejected(spec: ToolSpec, workspace_root: Path) -> None:
    _rejected(spec, workspace_root, spec.arguments() | {"unexpected": True})


@pytest.mark.parametrize("spec", PATTERN_TOOLS, ids=_ids(PATTERN_TOOLS))
def test_nul_in_pattern_is_rejected(spec: ToolSpec, workspace_root: Path) -> None:
    _rejected(spec, workspace_root, spec.arguments() | {"pattern": "a\0b"})


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
def test_nul_in_path_is_rejected(spec: ToolSpec, workspace_root: Path) -> None:
    _rejected(spec, workspace_root, spec.arguments() | {"path": "a\0b.txt"})


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
def test_non_string_path_is_rejected(spec: ToolSpec, workspace_root: Path) -> None:
    _rejected(spec, workspace_root, spec.arguments() | {"path": 1})


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
def test_parent_traversal_is_rejected_without_effect(
    spec: ToolSpec, workspace_root: Path
) -> None:
    outside = workspace_root.parent / "outside.txt"

    _rejected(spec, workspace_root, spec.arguments() | {"path": "../outside.txt"})

    assert outside.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
def test_absolute_outside_path_is_a_tool_target_not_a_preflight_rejection(
    spec: ToolSpec, workspace_root: Path
) -> None:
    """An absolute path outside the Workspace is resolved, not refused outright.

    Reading outside the Workspace is allowed; a write is refused later because
    no grant covers it. Either way the path is a legitimate target, so preflight
    no longer rejects it as malformed.
    """
    outside = workspace_root.parent / "outside.txt"

    tool = spec.build(Workspace(workspace_root))
    arguments = spec.arguments() | {"path": str(outside)}

    # Preflight only checks that the path is a well-formed target. Whether the
    # access is permitted is decided at execution time: a read is allowed, a
    # write needs a grant the session has not approved here.
    tool.validate(arguments)

    if tool.requested_capability(arguments) is not None:
        with pytest.raises(PermissionRefused, match="requires approval"):
            asyncio.run(tool.execute(arguments))

    assert outside.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
@pytest.mark.parametrize("metadata", [".git/config", ".GIT/config", ".Git/config"])
def test_git_metadata_path_is_rejected(
    spec: ToolSpec, workspace_root: Path, metadata: str
) -> None:
    """Git metadata stays unreachable whatever case the filesystem folds.

    A case-insensitive filesystem resolves ``.GIT/config`` to the same file as
    ``.git/config``, so an exact-case comparison would let a tool edit the
    metadata the sandbox is required to protect.
    """
    _rejected(spec, workspace_root, spec.arguments() | {"path": metadata})

    assert (workspace_root / ".git" / "config").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
def test_symlinked_file_is_rejected(spec: ToolSpec, workspace_root: Path) -> None:
    _rejected(spec, workspace_root, spec.arguments() | {"path": "link.txt"})

    outside = workspace_root.parent / "outside.txt"
    assert outside.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("spec", PATH_TOOLS, ids=_ids(PATH_TOOLS))
@pytest.mark.parametrize(
    "symlink_path", ["alias1/alias2/config", "real_dir/alias2/config"]
)
def test_nested_symlink_component_is_rejected(
    spec: ToolSpec, workspace_root: Path, symlink_path: str
) -> None:
    """A link anywhere on the path rejects it, not only the final component.

    The reason is pinned to the symlink so an incidental rejection (for example
    a missing file) cannot make this case pass.
    """
    _rejected(
        spec,
        workspace_root,
        spec.arguments() | {"path": symlink_path},
        match="symlink",
    )

    assert (workspace_root / ".git" / "config").read_text(encoding="utf-8") == "keep"


CREATING_TOOLS = tuple(
    spec for spec in TOOLS if spec.name in ("edit_file", "write_file")
)


@pytest.mark.parametrize("spec", CREATING_TOOLS, ids=_ids(CREATING_TOOLS))
@pytest.mark.parametrize("parent", ["alias1/alias2", "real_dir/alias2"])
def test_creating_a_file_through_a_nested_symlink_is_rejected(
    spec: ToolSpec, workspace_root: Path, parent: str
) -> None:
    """A creation cannot route around containment by targeting a new file."""
    arguments = (
        {"path": f"{parent}/new/file", "old_text": "", "new_text": "bad"}
        if spec.name == "edit_file"
        else {"path": f"{parent}/new/file", "content": "bad"}
    )

    _rejected(spec, workspace_root, arguments)

    assert not (workspace_root / ".git" / "new").exists()
    assert (workspace_root / ".git" / "config").read_text(encoding="utf-8") == "keep"
