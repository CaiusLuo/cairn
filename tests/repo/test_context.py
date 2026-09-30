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


def test_repo_context_reports_clean_main_branch(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    workspace = Workspace(tmp_path)

    context = asyncio.run(RepoContextProvider(workspace).inspect())

    assert context == RepositoryContext(
        workspace_root=workspace.root,
        repository_root=workspace.root,
        branch="main",
        dirty=False,
    )
    assert context.is_git_repository is True


def test_repo_context_separates_tracked_and_untracked_files(tmp_path: Path) -> None:
    tracked = _initialize_repository(tmp_path)
    tracked.write_text("after\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    context = asyncio.run(RepoContextProvider(workspace).inspect())

    assert context.repository_root == workspace.root
    assert context.branch == "main"
    assert context.dirty is True
    assert context.changed_files == ("tracked.txt",)
    assert context.untracked_files == ("untracked.txt",)
    assert context.truncated is False
    prompt = context.to_prompt()
    assert "tracked.txt" in prompt
    assert "untracked.txt" in prompt


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


def test_repo_context_truncates_paths_at_max_paths(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    for index in range(55):
        (tmp_path / f"untracked-{index:02}.txt").write_text("new\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    context = asyncio.run(RepoContextProvider(workspace, max_paths=50).inspect())

    assert context.changed_files == ()
    assert len(context.untracked_files) == 50
    assert all(path.startswith("untracked-") for path in context.untracked_files)
    assert context.truncated is True
    assert len(context.changed_files) + len(context.untracked_files) <= 50
    assert "File list truncated: yes" in context.to_prompt()
