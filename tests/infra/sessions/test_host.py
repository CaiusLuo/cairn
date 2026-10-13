"""Trusted observations distinguish identity, changing refs and broken registration."""

from pathlib import Path

import pytest

from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.workspace.workspace import Workspace
from tests.support.git import git, initialize_repository


def test_plain_workspace_and_corrupt_git_fail_closed(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    assert observe_workspace(workspace, frozenset()).git is None
    (tmp_path / ".git").write_text("invalid registration")
    with pytest.raises(SessionStoreError):
        observe_workspace(workspace, frozenset())


def test_linked_worktree_identity_and_changed_head(tmp_path: Path) -> None:
    repo, linked = tmp_path / "repo", tmp_path / "linked"
    repo.mkdir()
    initialize_repository(repo)
    git(repo, "worktree", "add", "-b", "linked", str(linked))
    child = linked / "child"
    child.mkdir()
    before = observe_workspace(Workspace(child), frozenset())
    assert before.git is not None
    assert before.git.workspace_prefix == "child/"
    assert before.git.common.path == str(repo / ".git")
    git(linked, "commit", "--allow-empty", "-m", "next")
    after = observe_workspace(Workspace(child), frozenset())
    assert after.git is not None
    assert after.git.head != before.git.head
    assert after.git.registration_digest == before.git.registration_digest
    assert after.git.admin == before.git.admin
    (Path(after.git.admin.path) / "gitdir").write_text(str(repo / ".git"))
    with pytest.raises(SessionStoreError):
        observe_workspace(Workspace(child), frozenset())


def test_unborn_and_host_git_environment_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("GIT_WORK_TREE", "/")
    binding = observe_workspace(Workspace(tmp_path), frozenset())
    assert binding.git is not None
    assert binding.git.head is None
    assert binding.git.branch == "main"
