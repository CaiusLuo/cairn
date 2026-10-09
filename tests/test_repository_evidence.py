import asyncio
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from cairn.repository import GIT_OUTPUT_CAPTURE_LIMIT, RepoContextProvider
from cairn.workspace.workspace import Workspace
from tests.test_repository import _git, _initialize_repository


@pytest.mark.parametrize("head", ["branch", "detached", "unborn"])
def test_snapshot_records_clean_head_states(tmp_path: Path, head: str) -> None:
    revision: str | None = None
    if head == "unborn":
        _git(tmp_path, "init", "-b", "main")
    else:
        _initialize_repository(tmp_path)
        revision = _git(tmp_path, "rev-parse", "HEAD")
        if head == "detached":
            _git(tmp_path, "checkout", "--detach", revision)
    evidence = asyncio.run(RepoContextProvider(Workspace(tmp_path)).inspect_evidence())
    assert evidence.is_git_repository is True
    assert evidence.branch == (None if head == "detached" else "main")
    assert evidence.head_revision == revision
    assert evidence.dirty is False
    assert evidence.changed_files == evidence.untracked_files == ()
    assert evidence.inspection_error is None


def test_snapshot_paths_are_unquoted_and_workspace_relative(tmp_path: Path) -> None:
    tracked = _initialize_repository(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    renamed = nested / 'space "雪"\nfile.txt'
    _git(tmp_path, "mv", str(tracked), str(renamed))
    directory = nested / "new directory"
    directory.mkdir()
    (directory / "b.txt").write_text("b")
    (directory / "a.txt").write_text("a")
    evidence = asyncio.run(RepoContextProvider(Workspace(nested)).inspect_evidence())
    assert evidence.inspection_error is None
    assert evidence.repository_root == tmp_path.resolve()
    assert evidence.changed_files == (renamed.name,)
    assert evidence.untracked_files == (
        "new directory/a.txt",
        "new directory/b.txt",
    )
    assert evidence.dirty is True


def test_snapshot_paths_have_deterministic_bounded_truncation(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    for index in reversed(range(80)):
        (tmp_path / f"untracked-{index:03}.txt").write_text("new")
    provider = RepoContextProvider(Workspace(tmp_path), max_paths=3)
    first = asyncio.run(provider.inspect_evidence())
    second = asyncio.run(provider.inspect_evidence())
    assert first == second
    assert first.untracked_files == (
        "untracked-000.txt",
        "untracked-001.txt",
        "untracked-002.txt",
    )
    assert first.dirty is True
    assert first.truncated is True
    assert first.path_limit == 3
    assert first.inspection_error is None


@pytest.mark.parametrize(
    "failure", ["corrupt_config", "corrupt_marker", "corrupt_head", "broken_link"]
)
def test_repository_failures_are_not_classified_as_non_git(
    tmp_path: Path, failure: str
) -> None:
    _initialize_repository(tmp_path)
    if failure == "corrupt_config":
        (tmp_path / ".git" / "config").write_text("[broken configuration\n")
    elif failure == "corrupt_marker":
        (tmp_path / ".git" / "HEAD").unlink()
    elif failure == "broken_link":
        (tmp_path / ".git").rename(tmp_path / "saved-git")
        (tmp_path / ".git").symlink_to(tmp_path / "missing-git")
    else:
        (tmp_path / ".git" / "refs" / "heads" / "main").write_text("0" * 40 + "\n")
    evidence = asyncio.run(RepoContextProvider(Workspace(tmp_path)).inspect_evidence())
    assert evidence.is_git_repository is not False
    assert evidence.inspection_error is not None


def test_status_failure_preserves_discovered_facts(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    revision = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / ".git" / "index").write_bytes(b"bad index")
    evidence = asyncio.run(RepoContextProvider(Workspace(tmp_path)).inspect_evidence())
    assert evidence.is_git_repository is True
    assert evidence.head_revision == revision
    assert evidence.branch == "main"
    assert evidence.dirty is None
    assert evidence.inspection_error is not None
    assert "git status failed" in evidence.inspection_error


def test_missing_git_is_an_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-programs"))
    evidence = asyncio.run(RepoContextProvider(Workspace(tmp_path)).inspect_evidence())
    assert evidence.is_git_repository is None
    assert evidence.inspection_error is not None
    assert "FileNotFoundError" in evidence.inspection_error


@pytest.mark.parametrize("cancel", [False, True])
def test_snapshot_timeout_and_cancellation_reap_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        processes: list[asyncio.subprocess.Process] = []
        provider = RepoContextProvider(Workspace(tmp_path), timeout=0.05)

        async def start_git(
            *args: str, env: Mapping[str, str] | None = None
        ) -> asyncio.subprocess.Process:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(provider, "_start_git", start_git)
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(provider.inspect_evidence())
        await started.wait()
        if cancel:
            task.cancel("snapshot cancelled")
            with pytest.raises(asyncio.CancelledError, match="snapshot cancelled"):
                await task
        else:
            evidence = await task
            assert evidence.is_git_repository is None
            assert evidence.inspection_error == "TimeoutError: "
        assert len(processes) == 1
        assert processes[0].returncode is not None
        assert asyncio.all_tasks() == baseline

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_snapshot_output_limit_remains_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def start_git(
        *args: str, env: Mapping[str, str] | None = None
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('x' * {GIT_OUTPUT_CAPTURE_LIMIT + 1})",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    provider = RepoContextProvider(Workspace(tmp_path))
    monkeypatch.setattr(provider, "_start_git", start_git)
    evidence = asyncio.run(provider.inspect_evidence())
    assert evidence.inspection_error == (
        "RuntimeError: Git inspection output exceeded capture limit"
    )
    assert evidence.is_git_repository is None
