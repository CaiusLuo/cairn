import os
from pathlib import Path

import pytest

from cairn.git import WorktreeProvider
from cairn.workspace.workspace import Workspace
from tests.git.helpers import git


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Cairn Tests")
    git(root, "config", "user.email", "cairn-tests@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / "file.txt").write_text("initial\n")
    (root / ".gitignore").write_text("ignored\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "tag", "base")
    (root / "file.txt").write_text("latest\n")
    git(root, "commit", "-am", "latest")
    return Workspace(root)


@pytest.fixture
def provider(source: Workspace, tmp_path: Path) -> WorktreeProvider:
    return WorktreeProvider(source, tmp_path / "worktrees")
