import subprocess
from pathlib import Path

from cairn.git.worktree import WorktreeHandle, WorktreeState
from cairn.workspace.workspace import Workspace


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def assert_removed(handle: WorktreeHandle, source: Workspace) -> None:
    assert not handle.path.exists()
    assert str(handle.path) not in git(source.root, "worktree", "list", "--porcelain")
    assert handle.state == WorktreeState.RELEASED
