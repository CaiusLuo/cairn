import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.git.context import RepoContextProvider, RepositoryContext
from cairn.infra.workspace.workspace import Workspace
from tests.support.git import git as _git
from tests.support.git import initialize_repository as _initialize_repository


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


def test_prompt_inspection_never_executes_repository_configuration(
    tmp_path: Path,
) -> None:
    """Read-only inspection must not run a program the repository supplies.

    ``core.fsmonitor`` is a program Git runs for ``git status``. Prompt
    inspection happens on every turn with this process's environment, so a
    repository — or an earlier sandboxed tool call writing ``.git/config`` —
    could otherwise win unsandboxed execution.
    """
    _initialize_repository(tmp_path)
    marker = tmp_path / "monitor-executed"
    monitor = tmp_path / "monitor"
    monitor.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n", encoding="utf-8"
    )
    monitor.chmod(0o755)
    _git(tmp_path, "config", "core.fsmonitor", str(monitor))
    (tmp_path / "dirty.txt").write_text("changed\n", encoding="utf-8")

    context = asyncio.run(RepoContextProvider(Workspace(tmp_path)).inspect())

    assert context.is_git_repository is True
    assert context.dirty is True
    assert not marker.exists()
    # Positive control: the same repository configuration does execute under
    # ordinary Git, so the absence above is suppression, not a broken fixture.
    _git(tmp_path, "status", "--porcelain")
    assert marker.exists()


def test_git_children_are_hardened_and_credential_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git child carries the overrides and an isolated environment."""
    captured: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class _Captured(Exception):
        pass

    async def capture(*command: str, **kwargs: Any) -> Any:
        captured.append((command, kwargs))
        raise _Captured

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setenv("XDG_CONFIG_HOME", "programmatic-credential")
    provider = RepoContextProvider(
        Workspace(tmp_path), secret_env_keys=frozenset({"XDG_CONFIG_HOME"})
    )

    with pytest.raises(_Captured):
        asyncio.run(provider._start_git("rev-parse", "--show-toplevel"))

    command, kwargs = captured[0]
    assert command[:2] == ("git", "-C")
    assert "core.hooksPath=/dev/null" in command
    assert "core.fsmonitor=false" in command
    child_env = kwargs["env"]
    assert "XDG_CONFIG_HOME" not in child_env
    assert "CAIRN_LLM_API_KEY" not in child_env
    assert child_env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.parametrize("operation", ["command", "status", "nul-status"])
@pytest.mark.parametrize("interruption", ["cancel", "timeout", "spawn-cancel"])
def test_inspection_reaps_child_and_readers_under_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    interruption: str,
) -> None:
    original_spawn = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    ready = tmp_path / "ready"
    # The child ignores termination and can fill a pipe before spawning returns.
    script = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).touch(); "
        "os.write(2, b'x' * 200000); time.sleep(60)"
    )

    async def scenario() -> None:
        spawned = asyncio.Event()
        allow_spawn_return = asyncio.Event()
        terminating = asyncio.Event()

        async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await original_spawn(
                sys.executable, "-c", script, str(ready), **kwargs
            )
            processes.append(process)
            while not ready.exists():
                await asyncio.sleep(0.001)
            spawned.set()
            if interruption == "spawn-cancel":
                await allow_spawn_return.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        provider = RepoContextProvider(Workspace(tmp_path), timeout=0.05)
        original_terminate = provider._terminate

        async def terminate(process: asyncio.subprocess.Process) -> None:
            terminating.set()
            await original_terminate(process)

        monkeypatch.setattr(provider, "_terminate", terminate)
        task: asyncio.Task[Any]
        if operation == "command":
            task = asyncio.create_task(provider.run_git("status"))
        else:
            task = asyncio.create_task(
                provider._read_status(nul=operation == "nul-status")
            )
        try:
            async with asyncio.timeout(5):
                await spawned.wait()
                if interruption != "timeout":
                    task.cancel("first cancellation")
                if interruption == "spawn-cancel":
                    await asyncio.sleep(0)
                    task.cancel("second cancellation")
                    allow_spawn_return.set()
                else:
                    await terminating.wait()
                    task.cancel("second cancellation")
                    await asyncio.sleep(0)
                    task.cancel("third cancellation")
                with pytest.raises(
                    TimeoutError
                    if interruption == "timeout"
                    else asyncio.CancelledError,
                    match=None if interruption == "timeout" else "first cancellation",
                ):
                    await task
                process = processes[0]
                assert process.returncode == -signal.SIGKILL
                with pytest.raises(ProcessLookupError):
                    os.kill(process.pid, 0)
                assert not [
                    pending
                    for pending in asyncio.all_tasks()
                    if pending is not asyncio.current_task()
                ]
        finally:
            # A failing regression must not leave its real subprocess behind.
            for process in processes:
                if process.returncode is None:
                    process.kill()
            pending = [
                child
                for child in asyncio.all_tasks()
                if child is not asyncio.current_task()
            ]
            for child in pending:
                child.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for process in processes:
                await process.communicate()

    asyncio.run(scenario())
