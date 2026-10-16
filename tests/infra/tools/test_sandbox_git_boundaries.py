"""Sandbox boundaries that protect Git metadata from the Bash tool.

The Bash tool grants write access to the workspace so ordinary source edits work.
Git metadata lives inside that workspace, and a linked worktree's ``.git`` file
points at an admin directory that may sit elsewhere on the host. Both paths must
stay read-only to the sandboxed command while read-only Git commands keep working.

These tests exercise the real ``BashTool.execute`` path, so they observe the
platform sandbox rather than a reimplementation of its arguments.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools.bash import (
    BashTool,
    UnprotectedGitMetadata,
    _git_metadata_paths,
)
from cairn.infra.workspace.workspace import Workspace
from tests.support.sandbox import require_working_sandbox

_MARKER_TEXT = "cairn-external-marker"
_PLATFORM: str = sys.platform


def _unmasked_external_root(tmp_path: Path) -> Path:
    """A host directory outside any path the sandbox already hides or shows.

    Linux replaces /tmp with an empty tmpfs, so a directory beside ``tmp_path``
    is invisible no matter what the mount list contains. A probe that has to
    discriminate a leaked mount therefore needs a root Linux does not mask.
    """
    if _PLATFORM == "linux":
        root = Path("/var/tmp") / f"cairn-sandbox-external-{os.getpid()}"
        root.mkdir(parents=True, exist_ok=True)
        return root
    root = tmp_path / "external"
    root.mkdir()
    return root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _direct_checkout(root: Path) -> Path:
    """A normal checkout whose Git directory sits inside the workspace."""
    workspace = root / "checkout"
    workspace.mkdir()
    _git("init", "-q", ".", cwd=workspace)
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git("add", ".", cwd=workspace)
    _git("commit", "-qm", "fixture", cwd=workspace)
    return workspace


def _linked_worktree(root: Path) -> tuple[Path, Path]:
    """A linked worktree plus the main repository holding its admin directory."""
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    linked = root / "linked"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    (repo / "shared.txt").write_text("shared\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "fixture", cwd=repo)
    _git("worktree", "add", "-q", str(linked), "-b", "task", cwd=repo)
    return repo, linked


def _run(root: Path, command: str) -> ToolResult:
    return asyncio.run(BashTool(Workspace(root)).execute({"command": command}))


def test_git_metadata_paths_cover_both_checkout_shapes(tmp_path: Path) -> None:
    """Direct checkouts expose .git; linked worktrees also expose the admin dir."""
    direct = _direct_checkout(tmp_path)
    assert _git_metadata_paths(direct) == ((direct / ".git").resolve(),)

    repo, linked = _linked_worktree(tmp_path / "linked-case")
    paths = {path.resolve() for path in _git_metadata_paths(linked)}
    assert (linked / ".git").resolve() in paths
    assert (repo / ".git").resolve() in paths
    assert (repo / ".git" / "worktrees" / "linked").resolve() in paths


def test_native_bash_cannot_write_a_direct_checkouts_git_directory(
    tmp_path: Path,
) -> None:
    workspace = _direct_checkout(tmp_path)
    require_working_sandbox(Workspace(workspace))
    head = (workspace / ".git" / "HEAD").read_bytes()

    marker = workspace / ".git" / "cairn-sandbox-probe"
    result = _run(workspace, "printf probe > .git/cairn-sandbox-probe")

    assert result.exit_code != 0, result.stdout
    assert not marker.exists()
    assert (workspace / ".git" / "HEAD").read_bytes() == head


def test_native_bash_cannot_remove_a_direct_checkouts_git_directory(
    tmp_path: Path,
) -> None:
    workspace = _direct_checkout(tmp_path)
    require_working_sandbox(Workspace(workspace))

    result = _run(workspace, "rm -rf .git")

    assert result.exit_code != 0, result.stdout
    assert (workspace / ".git").is_dir()
    assert (workspace / ".git" / "HEAD").is_file()


def test_native_bash_cannot_rename_a_direct_checkouts_git_directory(
    tmp_path: Path,
) -> None:
    workspace = _direct_checkout(tmp_path)
    require_working_sandbox(Workspace(workspace))

    result = _run(workspace, "mv .git moved-git")

    assert result.exit_code != 0, result.stdout
    assert (workspace / ".git").is_dir()
    assert not (workspace / "moved-git").exists()


def test_native_bash_cannot_rewrite_a_linked_worktrees_git_pointer(
    tmp_path: Path,
) -> None:
    _repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))
    before = (linked / ".git").read_bytes()

    result = _run(linked, "printf corrupted > .git")

    assert result.exit_code != 0, result.stdout
    assert (linked / ".git").read_bytes() == before


def test_native_bash_cannot_write_the_external_admin_directory(
    tmp_path: Path,
) -> None:
    """A worktree's admin directory sits outside the workspace and stays read-only."""
    repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))
    head = (repo / ".git" / "HEAD").read_bytes()

    result = _run(linked, f"printf probe > {repo / '.git' / 'cairn-sandbox-probe'}")

    assert result.exit_code != 0, result.stdout
    assert not (repo / ".git" / "cairn-sandbox-probe").exists()
    assert (repo / ".git" / "HEAD").read_bytes() == head


def test_native_bash_keeps_ordinary_source_files_writable(tmp_path: Path) -> None:
    workspace = _direct_checkout(tmp_path)
    require_working_sandbox(Workspace(workspace))

    result = _run(workspace, "printf written > new_file.txt")

    assert result.exit_code == 0, result.stderr
    assert (workspace / "new_file.txt").read_text(encoding="utf-8") == "written"


def test_native_bash_keeps_read_only_git_commands_working(tmp_path: Path) -> None:
    workspace = _direct_checkout(tmp_path)
    require_working_sandbox(Workspace(workspace))

    result = _run(workspace, "git log --oneline")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()


def test_native_bash_keeps_read_only_git_commands_working_in_a_worktree(
    tmp_path: Path,
) -> None:
    """Read-only Git needs the external admin directory, which must stay mounted."""
    repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))

    paths = {path.resolve() for path in _git_metadata_paths(linked)}
    assert (repo / ".git").resolve() in paths
    assert (linked / ".git").resolve() in paths

    result = _run(linked, "git log --oneline")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()


def test_git_metadata_paths_ignore_absent_entries(tmp_path: Path) -> None:
    """A workspace with no Git metadata mounts nothing extra."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert _git_metadata_paths(plain) == ()


def test_native_bash_cannot_rename_a_linked_worktrees_git_pointer(
    tmp_path: Path,
) -> None:
    _repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))
    before = (linked / ".git").read_bytes()

    result = _run(linked, "mv .git moved-git")

    assert result.exit_code != 0, result.stdout
    assert (linked / ".git").read_bytes() == before
    assert not (linked / "moved-git").exists()


def test_native_bash_cannot_replace_a_linked_worktrees_git_pointer(
    tmp_path: Path,
) -> None:
    """Replacing the pointer with a directory cannot unregister the worktree."""
    _repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))
    before = (linked / ".git").read_bytes()

    result = _run(linked, "rm .git && mkdir .git")

    assert result.exit_code != 0, result.stdout
    assert (linked / ".git").is_file()
    assert (linked / ".git").read_bytes() == before


def _forged_workspace(root: Path, pointer: str) -> Path:
    workspace = root / "forged"
    workspace.mkdir()
    (workspace / "src.txt").write_text("harmless\n", encoding="utf-8")
    (workspace / ".git").write_text(pointer + "\n", encoding="utf-8")
    return workspace


def test_git_metadata_paths_refuse_a_forged_absolute_gitdir(tmp_path: Path) -> None:
    """A workspace-authored pointer must be refused, not silently unprotected."""
    external = tmp_path / "elsewhere"
    external.mkdir()
    workspace = _forged_workspace(tmp_path, f"gitdir: {external}")

    with pytest.raises(UnprotectedGitMetadata):
        _git_metadata_paths(workspace)


def test_git_metadata_paths_refuse_a_forged_gitdir_with_a_matching_back_pointer(
    tmp_path: Path,
) -> None:
    """A planted back-pointer still fails the documented worktree layout."""
    external = tmp_path / "elsewhere"
    external.mkdir()
    workspace = _forged_workspace(tmp_path, f"gitdir: {external}")
    (external / "gitdir").write_text(f"{workspace / '.git'}\n", encoding="utf-8")
    (external / "commondir").write_text("..\n", encoding="utf-8")

    with pytest.raises(UnprotectedGitMetadata):
        _git_metadata_paths(workspace)


def test_git_metadata_paths_reject_an_in_workspace_forged_commondir(
    tmp_path: Path,
) -> None:
    """commondir cannot redirect a mount at the workspace's own parent."""
    workspace = tmp_path / "inner"
    admin = workspace / "worktrees" / "mine"
    admin.mkdir(parents=True)
    (workspace / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{workspace / '.git'}\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")

    paths = {path.resolve() for path in _git_metadata_paths(workspace)}

    assert tmp_path.resolve() not in paths


def test_git_metadata_paths_refuse_a_symlinked_git_entry(tmp_path: Path) -> None:
    """A symlinked .git can point into the writable workspace, so it is refused."""
    workspace = tmp_path / "symlinked"
    (workspace / "private-git").mkdir(parents=True)
    (workspace / ".git").symlink_to(workspace / "private-git")

    with pytest.raises(UnprotectedGitMetadata):
        _git_metadata_paths(workspace)


def test_git_metadata_paths_refuse_a_separate_git_dir(tmp_path: Path) -> None:
    """`git init --separate-git-dir` is not a worktree admin directory."""
    workspace = tmp_path / "separate"
    workspace.mkdir()
    _git(
        "init",
        "-q",
        "--separate-git-dir",
        str(workspace / "metadata"),
        ".",
        cwd=workspace,
    )
    assert (workspace / ".git").is_file()

    with pytest.raises(UnprotectedGitMetadata):
        _git_metadata_paths(workspace)


def test_native_bash_refuses_a_symlinked_git_entry(tmp_path: Path) -> None:
    """A symlinked .git must not let sandboxed Bash touch the real HEAD."""
    workspace = tmp_path / "symlinked"
    real_git = workspace / "private-git"
    real_git.mkdir(parents=True)
    _git("init", "-q", "--separate-git-dir", str(real_git), ".", cwd=workspace)
    (workspace / ".git").unlink()
    (workspace / ".git").symlink_to(real_git)
    head = (real_git / "HEAD").read_bytes()

    with pytest.raises(UnprotectedGitMetadata):
        _run(workspace, "printf corrupted > private-git/HEAD")

    assert (real_git / "HEAD").read_bytes() == head


def test_native_bash_refuses_a_separate_git_dir(tmp_path: Path) -> None:
    """`--separate-git-dir` metadata inside the workspace must not stay writable."""
    workspace = tmp_path / "separate"
    workspace.mkdir()
    _git(
        "init",
        "-q",
        "--separate-git-dir",
        str(workspace / "metadata"),
        ".",
        cwd=workspace,
    )
    head = (workspace / "metadata" / "HEAD").read_bytes()

    with pytest.raises(UnprotectedGitMetadata):
        _run(workspace, "printf corrupted > metadata/HEAD")

    assert (workspace / "metadata" / "HEAD").read_bytes() == head
    assert (workspace / ".git").read_text(encoding="utf-8").startswith("gitdir:")


def test_native_bash_refuses_before_spawning_a_command(tmp_path: Path) -> None:
    """Refusal is explicit and happens before any sandboxed process runs."""
    workspace = tmp_path / "separate"
    workspace.mkdir()
    _git(
        "init",
        "-q",
        "--separate-git-dir",
        str(workspace / "metadata"),
        ".",
        cwd=workspace,
    )
    marker = workspace / "should-not-exist.txt"

    with pytest.raises(UnprotectedGitMetadata) as refusal:
        _run(workspace, f"printf ran > {marker}")

    assert "pinned read-only" in str(refusal.value)
    assert not marker.exists()


def test_native_bash_cannot_read_a_forged_gitdir_target(tmp_path: Path) -> None:
    """A forged pointer must not widen what the sandbox can read on the host.

    Linux mounts a placeholder over /tmp, so a sibling of tmp_path would be
    invisible whether or not it leaked into the mount list. The probe therefore
    lives outside that placeholder on Linux. macOS allows reads by default, so
    the assertion there only records that the pointer adds no mount.
    """
    external = _unmasked_external_root(tmp_path)
    marker = external / "marker.txt"
    marker.write_text(_MARKER_TEXT, encoding="utf-8")
    forged = _forged_workspace(tmp_path, f"gitdir: {external}")

    with pytest.raises(UnprotectedGitMetadata):
        _git_metadata_paths(forged)
    with pytest.raises(UnprotectedGitMetadata):
        _run(forged, f"cat {marker}")

    if _PLATFORM != "linux":
        return
    # Control: a workspace with no Git metadata runs, and the host directory is
    # still not mounted, so the marker stays unreadable.
    control = tmp_path / "control"
    control.mkdir()
    require_working_sandbox(Workspace(control))
    result = _run(control, f"cat {marker}")

    assert _MARKER_TEXT not in result.stdout
