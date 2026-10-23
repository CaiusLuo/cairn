import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.infra.tools.bash import (
    BashTool,
    _decode_output,
    workspace_python,
)
from cairn.infra.tools.capture import PIPE_READ_CHUNK_SIZE, read_bounded
from cairn.infra.workspace.workspace import Workspace

_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
_LARGE_OUTPUT_CHUNK_BYTES = 64 * 1024
_LARGE_OUTPUT_CHUNKS = 32


def _completed_stream(data: bytes) -> AsyncMock:
    stream = AsyncMock(spec=asyncio.StreamReader)
    stream.read.side_effect = (data, b"")
    return stream


async def wait_for_path(path: Path, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not path.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


async def wait_for_process_group_exit(pgid: int) -> None:
    async def group_members() -> list[str]:
        process = await _CREATE_SUBPROCESS_EXEC(
            "/bin/ps",
            "-axo",
            "pid=,pgid=,stat=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return [
            line
            for line in stdout.decode().splitlines()
            if len(parts := line.split()) >= 2 and int(parts[1]) == pgid
        ]

    async def wait() -> None:
        while await group_members():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=2.0)


@pytest.mark.parametrize("ending", ["normal", "timeout", "cancel", "parent-exited"])
def test_bash_cleanup_removes_only_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    async def scenario() -> None:
        started = tmp_path / "started.txt"
        survived = tmp_path / "survived.txt"
        create_process = asyncio.create_subprocess_exec
        owned: list[asyncio.subprocess.Process] = []

        async def track_process(
            *args: str, **kwargs: Any
        ) -> asyncio.subprocess.Process:
            # Exercise real process ownership independently of sandbox wrappers.
            assert args[-5:-1] == ("/bin/bash", "-o", "pipefail", "-c")
            process = await create_process(*args[-5:], **kwargs)
            owned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)
        monkeypatch.setattr("cairn.infra.tools.bash.sys.platform", "darwin")
        tool = BashTool(
            Workspace(tmp_path),
            timeout=0.5 if ending in {"timeout", "parent-exited"} else 30.0,
            cleanup_timeout=0.5,
        )
        command = (
            "(printf started > started.txt; sleep 30; printf leaked > survived.txt) "
        )
        if ending == "normal":
            # Detached output lets the shell finish while its child is still alive.
            command += (
                ">/dev/null 2>&1 & "
                "while [ ! -f started.txt ]; do sleep 0.01; done; printf done"
            )
        elif ending == "parent-exited":
            command += "& exit 0"
        else:
            # Both descendants keep the shell's stdout/stderr pipes open.
            command += "& wait"

        unrelated = await create_process("/bin/sleep", "30", start_new_session=True)
        task = asyncio.create_task(tool.execute({"command": command}))
        try:
            await wait_for_path(started)
            if ending == "parent-exited":

                async def wait_for_parent_exit() -> None:
                    while owned[0].returncode is None:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_parent_exit(), timeout=0.25)
                assert not task.done(), "Descendant should still hold the output pipes"
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2.0)
            else:
                result = await asyncio.wait_for(task, timeout=2.0)
                assert result.exit_code == (0 if ending == "normal" else -1)
                if ending == "normal":
                    assert result.stdout == "done"

            assert len(owned) == 1
            assert owned[0].returncode is not None
            await wait_for_process_group_exit(owned[0].pid)
            assert not survived.exists()
            assert unrelated.returncode is None
            os.killpg(unrelated.pid, 0)
        finally:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                for process in [*owned, unrelated]:
                    with suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                for process in [*owned, unrelated]:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=2.0)

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
def test_bash_local_shell_cleans_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """A timed-out or cancelled command leaves no descendant behind.

    There is no OS sandbox: the command runs directly as the user, so this
    asserts the local process-group cleanup rather than a sandbox wrapper.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX process groups are required")

    async def scenario() -> None:
        tool = BashTool(Workspace(tmp_path), timeout=0.5, cleanup_timeout=0.5)
        preflight = await tool.execute({"command": "printf shell-ready"})
        assert preflight.exit_code == 0, preflight.stderr
        assert preflight.stdout == "shell-ready"

        create_process = asyncio.create_subprocess_exec
        owned: list[asyncio.subprocess.Process] = []

        async def track_process(
            *args: str, **kwargs: Any
        ) -> asyncio.subprocess.Process:
            # Preserve the real argv and process-group ownership.
            process = await create_process(*args, **kwargs)
            owned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)
        started = tmp_path / "started.txt"
        survived = tmp_path / "survived.txt"
        command = (
            "(printf started > started.txt; sleep 30; "
            "printf leaked > survived.txt) & wait"
        )
        task = asyncio.create_task(tool.execute({"command": command}))
        try:
            await wait_for_path(started)
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2.0)
            else:
                result = await asyncio.wait_for(task, timeout=2.0)
                assert result.exit_code == -1

            assert len(owned) == 1
            assert owned[0].returncode is not None
            await wait_for_process_group_exit(owned[0].pid)
            assert not survived.exists()
        finally:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                for process in owned:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                for process in owned:
                    await asyncio.wait_for(process.wait(), timeout=2.0)

    asyncio.run(scenario())


def test_bash_tool_child_inherits_host_env_and_keeps_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("TMPDIR", "/host/tmp")
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")
    monkeypatch.setenv("CAIRN_TEST_PASSTHROUGH", "preserved")

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {
                "command": (
                    'printf "%s|%s|%s|%s|%s" "$PWD" "$HOME" "$TMPDIR" '
                    '"${CAIRN_LLM_API_KEY:-}" "$CAIRN_TEST_PASSTHROUGH"'
                )
            }
        )
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{tmp_path}|/host/home|/host/tmp||preserved"


def test_bash_tool_preserves_command_text_and_decodes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    process.pid = 12345
    process.returncode = 3
    process.stdout = _completed_stream(b"before\xffafter")
    process.stderr = _completed_stream(b"warning:\xff")
    process.wait.return_value = 3
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("cairn.infra.tools.bash.os.killpg", Mock())
    monkeypatch.setattr("cairn.infra.tools.bash.sys.platform", "darwin")
    command = "  printf '%s' 'hello'\n"

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute({"command": command})
    )

    create_process.assert_awaited_once()
    assert create_process.call_args.args[-5:] == (
        "/bin/bash",
        "-o",
        "pipefail",
        "-c",
        command,
    )
    assert result.stdout == "before\ufffdafter"
    assert result.stderr == "warning:\ufffd"
    assert result.exit_code == 3


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
def test_bash_collection_cleanup_error_preserves_primary_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    async def scenario() -> None:
        collection_started = asyncio.Event()
        release_collection = asyncio.Event()
        process = AsyncMock()
        process.pid = 12345

        class CleanupErrorBashTool(BashTool):
            async def _collect_output(
                self,
                owned_process: asyncio.subprocess.Process,
            ) -> tuple[bytes, bool, bytes, bool, int]:
                assert owned_process is process
                collection_started.set()
                await release_collection.wait()
                raise RuntimeError("collection cleanup failed")

            async def _cleanup_process(
                self,
                owned_process: asyncio.subprocess.Process,
            ) -> None:
                assert owned_process is process
                release_collection.set()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr("cairn.infra.tools.bash.sys.platform", "darwin")
        tool = CleanupErrorBashTool(
            Workspace(tmp_path),
            timeout=0.01 if ending == "timeout" else 30.0,
            cleanup_timeout=0.1,
        )
        execution = asyncio.create_task(tool.execute({"command": "ignored"}))
        await collection_started.wait()

        if ending == "cancel":
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        else:
            result = await execution
            assert result.exit_code == -1
            assert "time out after 0.01s" in result.stderr

    asyncio.run(scenario())


def test_bash_cleanup_wait_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        process = AsyncMock()
        process.pid = 12345

        async def block_wait() -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process.wait.side_effect = block_wait

        monkeypatch.setattr(
            "cairn.infra.tools.bash.os.killpg",
            Mock(),
        )

        tool = BashTool(
            Workspace(tmp_path),
            cleanup_timeout=0.01,
        )

        await asyncio.wait_for(
            tool._cleanup_process(process),
            timeout=0.1,
        )

    asyncio.run(scenario())


def test_bash_tool_returns_stderr_and_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": "printf 'failure' >&2; exit 3"},
        )
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == "failure"
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_bash_pipeline_preserves_upstream_failure_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": "/bin/sh -c 'printf payload; exit 7' | tail"}
        )
    )

    assert result.exit_code == 7
    assert result.stdout == "payload"
    assert result.stderr == ""


def test_bash_drains_large_stdout_and_stderr_without_deadlock(
    tmp_path: Path,
) -> None:
    command = (
        f'i=0; while [ "$i" -lt {_LARGE_OUTPUT_CHUNKS} ]; do '
        + f"printf '%{_LARGE_OUTPUT_CHUNK_BYTES}s' x; "
        + f"printf '%{_LARGE_OUTPUT_CHUNK_BYTES}s' y >&2; "
        + "i=$((i + 1)); done"
    )
    stdout_limit = 128
    stderr_limit = 96

    result = asyncio.run(
        BashTool(
            Workspace(tmp_path),
            timeout=5.0,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
        ).execute({"command": command})
    )

    assert result.exit_code == 0
    assert result.stdout == " " * stdout_limit
    assert result.stderr == " " * stderr_limit
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_bounded_output_does_not_emit_partial_utf8_character() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()

        reader.feed_data("你你".encode())
        reader.feed_eof()

        data, truncated = await read_bounded(reader, 4)

        assert truncated is True
        assert _decode_output(data, truncated=True) == "你"

    asyncio.run(scenario())


def test_bounded_output_never_requests_unbounded_read() -> None:
    async def scenario() -> None:
        stream = AsyncMock(spec=asyncio.StreamReader)
        stream.read.side_effect = (
            b"a" * PIPE_READ_CHUNK_SIZE,
            b"b",
            b"",
        )

        data, truncated = await read_bounded(stream, 4)

        assert data == b"aaaa"
        assert truncated is True
        assert stream.read.await_count == 3
        assert all(
            read.args == (PIPE_READ_CHUNK_SIZE,) for read in stream.read.await_args_list
        )

    asyncio.run(scenario())


def _make_venv_python(root: Path, name: str = "python") -> Path:
    directory = root / ".venv" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    return executable


def test_workspace_python_prefers_the_workspace_venv(tmp_path: Path) -> None:
    """The project's own environment wins over however Cairn was launched."""
    executable = _make_venv_python(tmp_path)

    assert workspace_python(tmp_path) == str(executable)


def test_workspace_python_falls_back_to_python3_in_the_venv(tmp_path: Path) -> None:
    """A venv that only ships `python3` is still the project's environment."""
    executable = _make_venv_python(tmp_path, "python3")

    assert workspace_python(tmp_path) == str(executable)


def test_workspace_python_uses_the_running_interpreter_without_a_venv(
    tmp_path: Path,
) -> None:
    """With no project environment the funnel reaches the running process."""
    assert not (tmp_path / ".venv").exists()

    assert workspace_python(tmp_path) == sys.executable


def test_workspace_python_prefers_the_venv_over_the_running_interpreter(
    tmp_path: Path,
) -> None:
    """Both exist: the ordering, not just the presence, is what is asserted."""
    executable = _make_venv_python(tmp_path)

    selected = workspace_python(tmp_path)

    assert selected == str(executable)
    assert selected != sys.executable
