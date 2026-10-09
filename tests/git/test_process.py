import asyncio
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import cairn.git.worktree as module
from cairn.git.worktree import WorktreeError, WorktreeProvider, WorktreeState
from cairn.workspace.workspace import Workspace
from tests.git.helpers import assert_removed, git


@pytest.mark.parametrize("mode", ["timeout", "cancel", "spawn-cancel"])
def test_creation_stops_child_and_rolls_back(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    original = asyncio.create_subprocess_exec
    ready = tmp_path / "ready"
    processes: list[asyncio.subprocess.Process] = []
    # Run a real worktree add, then emulate a stuck Git/filter ignoring SIGTERM.
    script = (
        "import pathlib, signal, subprocess, sys, time; "
        "subprocess.run(sys.argv[2:], check=True); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    spawned = asyncio.Event()
    allow_spawn_return = asyncio.Event()

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if "add" in args and "worktree" in args:
            process = await original(
                sys.executable, "-c", script, str(ready), *args, **kwargs
            )
            processes.append(process)
            spawned.set()
            if mode == "spawn-cancel":
                await allow_spawn_return.wait()
            return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "TERMINATE_TIMEOUT", 0.03)
    if mode == "timeout":
        monkeypatch.setattr(module, "GIT_TIMEOUT", 0.5)

    async def scenario() -> None:
        task = asyncio.create_task(provider.create("base", "task"))
        await asyncio.wait_for(spawned.wait(), 5)
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        if mode != "timeout":
            task.cancel("external cancellation")
            await asyncio.sleep(0)
            allow_spawn_return.set()
            # Repeated cancellation must not detach subprocess/rollback cleanup.
            await asyncio.sleep(0.01)
            task.cancel("second cancellation")
        with pytest.raises(
            TimeoutError if mode == "timeout" else asyncio.CancelledError
        ):
            await task
        assert processes[0].returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        assert not list(provider.parent.iterdir())
        assert git(source.root, "branch", "--list", "task") == ""
        assert len(git(source.root, "worktree", "list").splitlines()) == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


def test_bounded_output_is_rejected_and_process_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(
            sys.executable,
            "-c",
            "import os; os.write(1, b'x' * 200000); os.write(2, b'y' * 200000)",
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(WorktreeError, match="capture limit"):
        asyncio.run(module._git(tmp_path, "status"))
    assert processes[0].returncode == 0


def test_reader_capture_bound() -> None:
    async def scenario() -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * (module.OUTPUT_LIMIT * 3))
        stream.feed_eof()
        data, truncated = await module._read(stream)
        assert data == b"x" * module.OUTPUT_LIMIT
        assert truncated
        assert stream.at_eof()

    asyncio.run(scenario())


@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_streaming_tree_scan_stops_child_and_rolls_back(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: bool,
    mode: str,
) -> None:
    original = asyncio.create_subprocess_exec
    ready = tmp_path / "scan-ready"
    processes: list[asyncio.subprocess.Process] = []
    scans = 0
    # Enumerate the real tree, then hold its pipe open while ignoring SIGTERM.
    script = (
        "import pathlib, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "subprocess.run(sys.argv[2:], check=True); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    )

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal scans
        if "ls-tree" in args:
            scans += 1
            if scans == (2 if target else 1):
                process = await original(
                    sys.executable, "-c", script, str(ready), *args, **kwargs
                )
                processes.append(process)
                return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "TERMINATE_TIMEOUT", 0.03)
    if mode == "timeout":
        monkeypatch.setattr(module, "GIT_TIMEOUT", 0.5)

    async def scenario() -> None:
        task = asyncio.create_task(provider.create("base", "task"))
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        if mode == "cancel":
            task.cancel("external scan cancellation")
            await asyncio.sleep(0.01)
            task.cancel("again")
        with pytest.raises(
            TimeoutError if mode == "timeout" else asyncio.CancelledError
        ):
            await task
        assert processes[0].returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        if target:
            assert not list(provider.parent.iterdir())
        else:
            assert not provider.parent.exists()
        assert git(source.root, "branch", "--list", "task") == ""
        assert len(git(source.root, "worktree", "list").splitlines()) == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


def test_filter_rejection_reaps_blocked_tree_producer(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (source.root / ".gitattributes").write_text("file.txt filter=lfs\n")
    git(source.root, "add", ".gitattributes")
    git(source.root, "commit", "-m", "filter attributes")
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    # More data than the pipe/StreamReader can buffer. The first batch rejects
    # while this producer remains blocked writing, with no reader left to drain.
    script = (
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdout.buffer.write(b'file.txt\\0' * 30000); "
        "sys.stdout.flush(); time.sleep(60)"
    )

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if "ls-tree" in args:
            process = await original(sys.executable, "-c", script, **kwargs)
            processes.append(process)
            return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "TERMINATE_TIMEOUT", 0.03)

    async def scenario() -> None:
        async with asyncio.timeout(5):
            with pytest.raises(WorktreeError, match=r"Git LFS.*pointer files") as error:
                await provider.create("HEAD", "task")
        assert not getattr(error.value, "__notes__", ())
        assert processes[0].returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        assert not provider.parent.exists()
        assert git(source.root, "branch", "--list", "task") == ""
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_streaming_scan_stops_producer_and_attribute_child(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    original = asyncio.create_subprocess_exec
    ready = tmp_path / "attributes-ready"
    processes: list[asyncio.subprocess.Process] = []
    producer = (
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdout.buffer.write(b'file.txt\\0' * 30000); "
        "sys.stdout.flush(); time.sleep(60)"
    )
    consumer = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    )

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        # Source preflight succeeds; inject failure in the registered target so
        # both process groups and owned worktree/branch must be cleaned up.
        if args[2] != str(source.root):
            if "ls-tree" in args:
                process = await original(sys.executable, "-c", producer, **kwargs)
                processes.append(process)
                return process
            if "check-attr" in args:
                process = await original(
                    sys.executable, "-c", consumer, str(ready), **kwargs
                )
                processes.append(process)
                return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "TERMINATE_TIMEOUT", 0.03)
    if mode == "timeout":
        monkeypatch.setattr(module, "GIT_TIMEOUT", 0.5)

    async def scenario() -> None:
        task = asyncio.create_task(provider.create("base", "task"))
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        if mode == "cancel":
            task.cancel("external batch cancellation")
            await asyncio.sleep(0.01)
            task.cancel("again")
        with pytest.raises(
            TimeoutError if mode == "timeout" else asyncio.CancelledError
        ):
            await task
        assert len(processes) == 2
        for process in processes:
            assert process.returncode == -signal.SIGKILL
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
        assert not list(provider.parent.iterdir())
        assert git(source.root, "branch", "--list", "task") == ""
        assert len(git(source.root, "worktree", "list").splitlines()) == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


@pytest.mark.parametrize("output", ["long-path", "partial-path", "stderr"])
def test_streaming_scan_retains_record_and_diagnostic_bounds(
    provider: WorktreeProvider,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    script = {
        "long-path": "import os; os.write(1, b'x' * 200000)",
        "partial-path": "import os; os.write(1, b'file.txt')",
        "stderr": "import os; os.write(1, b'file.txt\\0'); os.write(2, b'x' * 200000)",
    }[output]

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if "ls-tree" in args:
            process = await original(sys.executable, "-c", script, **kwargs)
            processes.append(process)
            return process
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario() -> None:
        message = "unterminated path" if output == "partial-path" else "capture limit"
        async with asyncio.timeout(5):
            with pytest.raises(WorktreeError, match=message):
                await provider.create("base", None)
        assert processes[0].returncode is not None
        assert not provider.parent.exists()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_cancel_during_release_finishes_observed_cleanup(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    original = module._git
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def pause(
        root: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> bytes:
        if args[:2] == ("worktree", "remove"):
            entered.set()
            await resume.wait()
            if cleanup_fails:
                raise OSError("cleanup failed during cancellation")
        return await original(root, *args, env=env)

    async def scenario() -> None:
        handle = await provider.create("base", "task")
        monkeypatch.setattr(module, "_git", pause)
        task = asyncio.create_task(handle.release())
        await entered.wait()
        with pytest.raises(WorktreeError, match="already in progress"):
            await handle.release()
        task.cancel("external")
        await asyncio.sleep(0)
        task.cancel("again")
        resume.set()
        with pytest.raises(asyncio.CancelledError, match="external") as error:
            await task
        if cleanup_fails:
            assert any(
                "cleanup failed during cancellation" in note
                for note in error.value.__notes__
            )
            assert handle.state == WorktreeState.ACTIVE
            assert handle.path.exists()
            monkeypatch.setattr(module, "_git", original)
            await handle.release()
        assert_removed(handle, source)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(scenario())


def test_branch_cleanup_failure_can_be_retried(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module._git

    async def fail(
        root: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> bytes:
        if args[:2] == ("update-ref", "-d"):
            raise OSError("ref cleanup failed")
        return await original(root, *args, env=env)

    async def scenario() -> None:
        handle = await provider.create("base", "task")
        monkeypatch.setattr(module, "_git", fail)
        with pytest.raises(OSError, match="ref cleanup"):
            await handle.release()
        assert handle.state == WorktreeState.ACTIVE
        assert not handle.path.exists()
        assert git(source.root, "branch", "--list", "task")
        monkeypatch.setattr(module, "_git", original)
        await handle.release()
        assert_removed(handle, source)
        assert git(source.root, "branch", "--list", "task") == ""

    asyncio.run(scenario())


def test_git_environment_cannot_redirect_source(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unowned"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unowned-index"))

    async def scenario() -> None:
        handle = await provider.create("base", None)
        assert (handle.path / "file.txt").read_text() == "initial\n"
        await handle.release()
        assert not handle.path.exists()

    asyncio.run(scenario())
    assert not (tmp_path / "unowned").exists()
    assert not (tmp_path / "unowned-index").exists()
