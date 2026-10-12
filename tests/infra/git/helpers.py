from cairn.infra.git.worktree import WorktreeHandle, WorktreeState
from cairn.infra.workspace.workspace import Workspace
from tests.support.git import git


def assert_removed(handle: WorktreeHandle, source: Workspace) -> None:
    assert not handle.path.exists()
    assert str(handle.path) not in git(source.root, "worktree", "list", "--porcelain")
    assert handle.state == WorktreeState.RELEASED
