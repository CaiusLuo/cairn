"""Sandbox boundaries that protect Git metadata from the Bash tool.

The Bash tool grants write access to the workspace so ordinary source edits work.
Git metadata lives inside that workspace, and a linked worktree's ``.git`` file
points at an admin directory that may sit elsewhere on the host. Both paths must
stay read-only to the sandboxed command while read-only Git commands keep working.

These tests exercise the real ``BashTool.execute`` path, so they observe the
platform sandbox rather than a reimplementation of its arguments.
"""

import asyncio
import subprocess
from pathlib import Path

from cairn.core.models import ToolResult
from cairn.infra.tools.bash import BashTool, _git_metadata_paths
from cairn.infra.workspace.workspace import Workspace
from tests.support.sandbox import require_working_sandbox


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
    _repo, linked = _linked_worktree(tmp_path)
    require_working_sandbox(Workspace(linked))

    result = _run(linked, "git log --oneline")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()


def test_git_metadata_paths_ignore_absent_entries(tmp_path: Path) -> None:
    """A workspace with no Git metadata mounts nothing extra."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert _git_metadata_paths(plain) == ()
