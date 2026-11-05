"""Bash owns a blocking sandbox inventory without blocking message handling."""

import asyncio
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cairn.infra import sandbox
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace


def test_slow_sandbox_inventory_keeps_loop_live_and_builds_argv_on_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = BashTool(Workspace(tmp_path), sandbox_shell=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        owner = threading.get_ident()
        marker = tmp_path / "executed"

        def inventory(_root: Path) -> list[Path]:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test did not release inventory")
            return []

        def argv(
            workspace: Workspace,
            command: str,
            *,
            empty_file_fd: Callable[[], int],
            private_paths: Sequence[Path] | None = None,
            seccomp_fd: Callable[[], int] | None = None,
        ) -> list[str]:
            assert threading.get_ident() == owner
            assert private_paths == []
            # A live FD proves allocation stayed within the owning ExitStack.
            assert empty_file_fd() >= 0
            return ["/bin/bash", "-c", command]

        monkeypatch.setattr(sandbox, "shell_private_paths", inventory)
        monkeypatch.setattr(sandbox, "build_shell_argv", argv)
        running = asyncio.create_task(tool.execute({"command": "touch executed"}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            progressed = asyncio.Event()
            loop.call_soon(progressed.set)
            await asyncio.wait_for(progressed.wait(), 1)
            assert not running.done(), "loop must progress while inventory waits"
            assert not marker.exists(), "command must wait for complete inventory"
            release.set()
            result = await running
            assert result.exit_code == 0, result.stderr
            assert marker.exists()
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_sandbox_inventory_settles_repeated_cancellation_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_fails: bool
) -> None:
    tool = BashTool(Workspace(tmp_path), sandbox_shell=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    spawn = AsyncMock(side_effect=AssertionError("cancelled inventory must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        def inventory(_root: Path) -> list[Path]:
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release inventory")
                if worker_fails:
                    raise OSError("inventory failed during cancellation")
                return []
            finally:
                finished.set()

        monkeypatch.setattr(sandbox, "shell_private_paths", inventory)
        running = asyncio.create_task(tool.execute({"command": "touch executed"}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            running.cancel("primary cancellation")
            await asyncio.sleep(0)
            running.cancel("repeated cancellation")
            await asyncio.sleep(0)
            assert not running.done(), "Bash must retain its scanning worker"
            assert not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError, match="primary cancellation"):
                await running
            assert finished.is_set()
            assert unhandled == []
            spawn.assert_not_called()
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_unreadable_inventory_fails_closed_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = BashTool(Workspace(tmp_path), sandbox_shell=True)
    monkeypatch.setattr(sandbox, "available", lambda: True)
    spawn = AsyncMock(side_effect=AssertionError("incomplete inventory must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def unreadable_walk(
        _root: Path,
        *,
        followlinks: bool,
        onerror: Callable[[OSError], None] | None = None,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        if onerror is not None:
            onerror(PermissionError("private scan failed"))
        return iter([])

    monkeypatch.setattr(os, "walk", unreadable_walk)
    result = asyncio.run(tool.execute({"command": "touch executed"}))
    assert result.error_type == "sandbox_unavailable"
    spawn.assert_not_called()
    assert not (tmp_path / "executed").exists()
