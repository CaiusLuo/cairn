import asyncio
import subprocess
from pathlib import Path

from cairn.repo.context import RepoContextProvider, RepositoryContext
from cairn.workspace.workspace import Workspace


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _initialize_repository(root: Path) -> Path:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Cairn Tests")
    _git(root, "config", "user.email", "cairn-tests@example.invalid")
    tracked = root / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(root, "add", "--", tracked.name)
    _git(root, "commit", "-m", "initial")
    return tracked


def test_repo_context_reports_non_git_workspace(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    context = asyncio.run(RepoContextProvider(workspace).inspect())

    assert context == RepositoryContext(
        workspace_root=workspace.root,
        repository_root=None,
        branch=None,
        dirty=None,
    )
    assert context.is_git_repository is False
    assert context.to_prompt() == (
        "Runtime workspace context:\n"
        f"- Workspace: {workspace.root}\n"
        "- Git repository: no"
    )


def test_repo_context_tracks_clean_dirty_and_truncated_states(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    workspace = Workspace(tmp_path)
    provider = RepoContextProvider(workspace, max_paths=50)

    clean = asyncio.run(provider.inspect())

    assert clean.repository_root == workspace.root
    assert clean.branch == "main"
    assert clean.dirty is False
    assert clean.is_git_repository is True

    tracked = tmp_path / "tracked.txt"
    tracked.write_text("after\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")

    dirty = asyncio.run(provider.inspect())

    assert dirty.dirty is True
    assert dirty.changed_files == ("tracked.txt",)
    assert dirty.untracked_files == ("untracked.txt",)
    assert dirty.truncated is False
    prompt = dirty.to_prompt()
    assert "tracked.txt" in prompt
    assert "untracked.txt" in prompt

    for index in range(55):
        (tmp_path / f"untracked-{index:02}.txt").write_text("new\n", encoding="utf-8")
    truncated = asyncio.run(provider.inspect())

    assert truncated.changed_files == ("tracked.txt",)
    assert len(truncated.untracked_files) == 49
    assert all(path.startswith("untracked-") for path in truncated.untracked_files)
    assert truncated.truncated is True
    assert len(truncated.changed_files) + len(truncated.untracked_files) == 50
    assert "File list truncated: yes" in truncated.to_prompt()


def test_repo_context_paths_are_relative_to_nested_workspace(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    _initialize_repository(repo)
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)
    tracked = nested / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(repo, "add", "--", "services/api/tracked.txt")
    _git(repo, "commit", "-m", "add nested file")
    tracked.write_text("after\n", encoding="utf-8")
    (nested / "new.txt").write_text("new\n", encoding="utf-8")
    workspace = Workspace(nested)

    context = asyncio.run(RepoContextProvider(workspace).inspect())

    assert context.repository_root == repo.resolve()
    assert context.workspace_root == nested.resolve()
    assert context.changed_files == ("tracked.txt",)
    assert context.untracked_files == ("new.txt",)
