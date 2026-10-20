"""Workspace containment for ``glob``.

``glob`` must never name a file outside the Workspace back to the model. Unlike
every other file tool it does not resolve a single target: it expands a pattern,
so containment is enforced in two layers.

1. ``GlobTool.validate`` rejects a pattern that can leave the Workspace before
   anything is scanned (absolute, ``..`` parts, ``.git`` parts, and a literal
   prefix that is a symlink).
2. ``GlobTool._search`` re-checks every candidate after resolution and drops the
   ones that are not provably inside the Workspace. This is the layer that
   covers the case layer 1 cannot see: a wildcard that expands to a symlinked
   directory.

Both layers are exercised here, plus positive controls proving ordinary
in-workspace globbing is unchanged.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.glob import GlobTool
from cairn.infra.workspace.paths import contained_candidate
from cairn.infra.workspace.workspace import Workspace


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def escape_layout(tmp_path: Path) -> Path:
    """A workspace with an in-tree file and an outside directory behind links."""
    workspace_root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    _write(outside / "secret.py", "SECRET = 'do not leak'\n")
    _write(outside / "nested" / "deep.py", "SECRET\n")

    _write(workspace_root / "inside.py", "ok\n")
    _write(workspace_root / "src" / "a.py", "ok\n")
    _write(workspace_root / "src" / "test_b.py", "ok\n")
    _write(workspace_root / ".git" / "config.py", "ok\n")

    # A symlinked directory named by a literal pattern prefix.
    (workspace_root / "link").symlink_to(outside, target_is_directory=True)
    # A symlinked subdirectory inside an otherwise ordinary tree.
    _write(workspace_root / "pkg" / "real.py", "ok\n")
    (workspace_root / "pkg" / "nested").symlink_to(outside, target_is_directory=True)
    return workspace_root


def _run(root: Path, pattern: str, **extra: object) -> ToolResult:
    tool = GlobTool(Workspace(root))
    arguments: dict[str, Any] = {"pattern": pattern} | extra
    return asyncio.run(tool.execute(arguments))


# --- layer 1: the pattern itself is refused -------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        "../outside/*.py",
        "**/../outside/*.py",
        "src/../../outside/*.py",
        ".git/*.py",
        "**/.git/*.py",
        "link/../*.py",
    ],
)
def test_pattern_that_leaves_the_workspace_is_rejected_as_an_argument(
    escape_layout: Path, pattern: str
) -> None:
    """A refused pattern is an argument failure, not an empty result.

    The model must learn that the pattern is not allowed; reporting "no matches"
    would hide the rejection and suggest the pattern was merely unlucky.
    """
    tool = GlobTool(Workspace(escape_layout))

    with pytest.raises(InvalidArguments):
        tool.validate({"pattern": pattern})
    with pytest.raises(InvalidArguments):
        asyncio.run(tool.execute({"pattern": pattern}))


@pytest.mark.parametrize("pattern", ["/etc/*.conf", "/tmp/*.py", str(Path("/") / "*")])
def test_absolute_pattern_is_rejected_as_an_argument(
    escape_layout: Path, pattern: str
) -> None:
    """An absolute pattern never reaches pathlib's NotImplementedError."""
    tool = GlobTool(Workspace(escape_layout))

    with pytest.raises(InvalidArguments):
        tool.validate({"pattern": pattern})


@pytest.mark.parametrize("pattern", [".", "./", ".//", "..", "//", "./."])
def test_pattern_naming_only_the_root_is_rejected_as_an_argument(
    escape_layout: Path, pattern: str
) -> None:
    """A root-only pattern is an argument error, not an internal crash.

    pathlib raises IndexError or AttributeError from inside ``glob`` for these
    patterns, which would surface to the model as an unclassified tool fault
    rather than a refused search.
    """
    tool = GlobTool(Workspace(escape_layout))

    with pytest.raises(InvalidArguments):
        tool.validate({"pattern": pattern})
    with pytest.raises(InvalidArguments):
        asyncio.run(tool.execute({"pattern": pattern}))


def test_literal_pattern_prefix_through_a_symlink_is_rejected(
    escape_layout: Path,
) -> None:
    """``link`` is a literal prefix, so layer 1 sees the link and refuses."""
    tool = GlobTool(Workspace(escape_layout))

    with pytest.raises(InvalidArguments, match="symlink"):
        tool.validate({"pattern": "link/*.py"})


def test_leading_recursive_wildcard_stays_allowed(escape_layout: Path) -> None:
    """``**/`` is the documented common case and must keep working."""
    tool = GlobTool(Workspace(escape_layout))

    tool.validate({"pattern": "**/*.py"})
    tool.validate({"pattern": "src/*/test_*.py"})
    tool.validate({"pattern": "*.py"})


# --- layer 2: the expanded candidates are re-checked ----------------------


@pytest.mark.parametrize("pattern", ["*/*.py", "*/secret.py", "**/*.py"])
def test_wildcard_never_reports_a_path_through_a_symlinked_directory(
    escape_layout: Path, pattern: str
) -> None:
    """These patterns pass layer 1; only the resolved-candidate check stops them.

    ``*/*.py`` and ``*/secret.py`` expand through the ``link`` entry, which a
    literal-prefix check cannot inspect because a wildcard precedes it.
    """
    result = _run(escape_layout, pattern)

    # Assert on reported paths, not on the message: a "no matches" reply echoes
    # the pattern back, and that echo is not a leak. A glob call always reports a
    # path list or the no-match notice, so both outcomes are checked here.
    for line in result.stdout.splitlines():
        if line.startswith("No workspace files match"):
            continue
        assert not line.endswith("secret.py"), line
        assert not line.endswith("deep.py"), line
        assert not line.startswith("outside/"), line


def test_nested_symlink_directory_inside_a_valid_tree_does_not_leak(
    escape_layout: Path,
) -> None:
    result = _run(escape_layout, "pkg/**/*.py")

    assert result.stdout.splitlines() == ["pkg/real.py"]
    assert "secret.py" not in result.stdout


def test_git_contents_are_never_reported(escape_layout: Path) -> None:
    """The ``.git`` exclusion survives both layers."""
    result = _run(escape_layout, "**/*.py")

    assert ".git" not in result.stdout
    assert "config.py" not in result.stdout


def test_contained_candidate_returns_none_for_an_outside_path(
    escape_layout: Path,
) -> None:
    """The shared helper fails closed for a path outside the root."""
    outside = escape_layout.parent / "outside" / "secret.py"
    workspace = Workspace(escape_layout)

    assert contained_candidate(workspace.root, outside) is None


def test_contained_candidate_returns_none_through_a_symlink_component(
    escape_layout: Path,
) -> None:
    workspace = Workspace(escape_layout)
    through_link = workspace.root / "link" / "secret.py"

    assert contained_candidate(workspace.root, through_link) is None


def test_contained_candidate_returns_the_path_for_a_contained_file(
    escape_layout: Path,
) -> None:
    workspace = Workspace(escape_layout)
    inside = workspace.root / "inside.py"

    assert contained_candidate(workspace.root, inside) == inside


def test_contained_candidate_drops_a_symlinked_file(escape_layout: Path) -> None:
    workspace = Workspace(escape_layout)
    outside_file = escape_layout.parent / "outside" / "secret.py"
    (workspace.root / "alias.py").symlink_to(outside_file)

    assert contained_candidate(workspace.root, workspace.root / "alias.py") is None


# --- positive controls: legitimate globbing is unchanged ------------------


def test_in_workspace_patterns_return_the_same_paths_as_before(
    escape_layout: Path,
) -> None:
    assert _run(escape_layout, "**/*.py").stdout.splitlines() == [
        "inside.py",
        "pkg/real.py",
        "src/a.py",
        "src/test_b.py",
    ]
    assert _run(escape_layout, "*.py").stdout.splitlines() == ["inside.py"]
    assert _run(escape_layout, "src/*.py").stdout.splitlines() == [
        "src/a.py",
        "src/test_b.py",
    ]
    assert _run(escape_layout, "src/*/test_*.py").stdout.startswith(
        "No workspace files match"
    )


def test_limit_and_truncation_contract_is_unchanged(escape_layout: Path) -> None:
    result = _run(escape_layout, "**/*.py", limit=2)

    assert len(result.stdout.splitlines()) == 2
    assert result.stdout_truncated is True


def test_missing_directory_still_reports_no_matches(escape_layout: Path) -> None:
    """A contained pattern that matches nothing is not an argument failure."""
    result = _run(escape_layout, "absent/**/*.py")

    assert result.exit_code == 0
    assert "No workspace files match" in result.stdout
