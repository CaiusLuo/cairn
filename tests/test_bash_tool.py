import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace

_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


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
            assert args[-3:-1] == ("/bin/sh", "-c")
            process = await create_process(*args[-3:], **kwargs)
            owned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", track_process)
        monkeypatch.setattr("cairn.tools.bash.sys.platform", "darwin")
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
def test_bash_sandbox_wrapper_cleans_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    if sys.platform == "linux" and not Path("/usr/bin/bwrap").is_file():
        pytest.skip("bubblewrap is not installed")

    async def scenario() -> None:
        tool = BashTool(Workspace(tmp_path), timeout=0.5, cleanup_timeout=0.5)
        preflight = await tool.execute({"command": "printf sandbox-ready"})
        namespace_errors = (
            "No permissions to create new namespace",
            "Creating new namespace failed: Operation not permitted",
            "setting up uid map: Permission denied",
        )
        if sys.platform == "linux" and any(
            error in preflight.stderr for error in namespace_errors
        ):
            pytest.skip(
                f"bubblewrap namespaces unavailable: {preflight.stderr.strip()}"
            )
        assert preflight.exit_code == 0, preflight.stderr
        assert preflight.stdout == "sandbox-ready"

        create_process = asyncio.create_subprocess_exec
        owned: list[asyncio.subprocess.Process] = []

        async def track_process(
            *args: str, **kwargs: Any
        ) -> asyncio.subprocess.Process:
            # Preserve the real sandbox-exec/bwrap argv and process-group ownership.
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


def test_bash_tool_schema_describes_required_command(tmp_path: Path) -> None:
    schema = BashTool(workspace=Workspace(tmp_path)).schema()

    assert schema["function"]["name"] == "bash"
    assert schema["function"]["parameters"]["required"] == ["command"]
    assert schema["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": None},
        {"command": 42},
        {"command": True},
        {"command": ""},
        {"command": " \t\n"},
        {"command": "pwd", "extra": True},
    ],
    ids=("missing", "none", "number", "boolean", "empty", "whitespace", "extra"),
)
def test_bash_tool_rejects_invalid_arguments_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    create_process = AsyncMock(
        side_effect=AssertionError("Invalid arguments reached subprocess creation")
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(ValueError, match="command"):
        asyncio.run(BashTool(workspace=Workspace(tmp_path)).execute(arguments))

    create_process.assert_not_called()


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code", "expected_stdout", "expected_stderr"),
    [
        (
            "中文输出".encode(),
            "中文错误".encode(),
            0,
            "中文输出",
            "中文错误",
        ),
        (b"before\xffafter", b"", 3, "before\ufffdafter", ""),
        (b"", b"warning:\xff", -1, "", "warning:\ufffd"),
    ],
    ids=("utf8", "invalid-stdout", "invalid-stderr"),
)
def test_bash_tool_preserves_command_text_and_decodes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    exit_code: int,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    process = AsyncMock()
    process.pid = 12345
    process.returncode = exit_code
    process.communicate.return_value = (stdout, stderr)
    process.wait.return_value = exit_code
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("cairn.tools.bash.os.killpg", Mock())
    monkeypatch.setattr("cairn.tools.bash.sys.platform", "darwin")
    command = "  printf '%s' 'hello'\n"

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute({"command": command})
    )

    create_process.assert_awaited_once()
    assert create_process.call_args.args[-3:] == ("/bin/sh", "-c", command)
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert result.exit_code == exit_code


def test_bash_tool_cancellation_kills_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        communicate_started = asyncio.Event()

        process = AsyncMock()
        process.pid = 12345
        process.returncode = None

        async def block_communicate() -> tuple[bytes, bytes]:
            communicate_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process.communicate.side_effect = block_communicate
        process.wait.return_value = -9

        create_process = AsyncMock(return_value=process)
        killpg = Mock()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            create_process,
        )
        monkeypatch.setattr(
            "cairn.tools.bash.os.killpg",
            killpg,
        )
        monkeypatch.setattr(
            "cairn.tools.bash.sys.platform",
            "darwin",
        )

        tool = BashTool(
            Workspace(tmp_path),
            timeout=30,
            cleanup_timeout=0.1,
        )

        task = asyncio.create_task(tool.execute({"command": "sleep 100"}))

        await communicate_started.wait()

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        killpg.assert_called_once_with(
            12345,
            signal.SIGKILL,
        )

        process.wait.assert_awaited_once()

        assert create_process.call_args.kwargs["start_new_session"] is True

    asyncio.run(scenario())


def test_bash_tool_timeout_cleans_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        process = AsyncMock()
        process.pid = 12345
        process.returncode = None

        async def block_communicate() -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process.communicate.side_effect = block_communicate
        process.wait.return_value = -9

        create_process = AsyncMock(return_value=process)
        killpg = Mock()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            create_process,
        )
        monkeypatch.setattr(
            "cairn.tools.bash.os.killpg",
            killpg,
        )
        monkeypatch.setattr(
            "cairn.tools.bash.sys.platform",
            "darwin",
        )

        tool = BashTool(
            Workspace(tmp_path),
            timeout=0.01,
            cleanup_timeout=0.1,
        )

        result = await tool.execute({"command": "sleep 100"})

        assert result.exit_code == -1
        assert "time out after 0.01s" in result.stderr

        killpg.assert_called_once_with(
            12345,
            signal.SIGKILL,
        )

        process.wait.assert_awaited_once()

        # communicate 只应该是原始执行那一次
        assert process.communicate.await_count == 1

    asyncio.run(scenario())


def test_bash_cleanup_tolerates_process_group_already_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        process = AsyncMock()
        process.pid = 12345
        process.wait.return_value = 0

        killpg = Mock(side_effect=ProcessLookupError)

        monkeypatch.setattr(
            "cairn.tools.bash.os.killpg",
            killpg,
        )

        tool = BashTool(
            Workspace(tmp_path),
            cleanup_timeout=0.1,
        )

        await tool._cleanup_process(process)

        killpg.assert_called_once_with(
            12345,
            signal.SIGKILL,
        )

        process.wait.assert_awaited_once()

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
            "cairn.tools.bash.os.killpg",
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


def test_bash_tool_executes_in_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = BashTool(workspace=Workspace(tmp_path))
    monkeypatch.chdir(tmp_path.parent)

    result = asyncio.run(tool.execute({"command": "pwd"}))

    assert result.exit_code == 0
    assert Path(result.stdout.strip()) == tmp_path
    assert result.stderr == ""


def test_auto_allowed_command_does_not_use_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ls = tmp_path / "ls"
    fake_ls.write_text("#!/bin/sh\nprintf hacked > pwned\n", encoding="utf-8")
    fake_ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute({"command": "ls"})
    )

    assert result.exit_code == 0
    assert not (tmp_path / "pwned").exists()


def test_bash_tool_returns_stderr_and_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": "printf 'failure' >&2; exit 3"},
        )
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == "failure"


def test_bash_tool_kills_timed_out_process(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path), timeout=0.01).execute(
            {"command": "sleep 1"}
        )
    )

    assert result.exit_code == -1
    assert "time out after 0.01s" in result.stderr


def test_bash_tool_confines_files_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    tool = BashTool(workspace=Workspace(tmp_path))

    read = asyncio.run(tool.execute({"command": f"cat {outside}"}))
    asyncio.run(tool.execute({"command": f"printf data > {outside}"}))
    inside = asyncio.run(tool.execute({"command": "printf data > inside.txt"}))

    assert read.exit_code != 0
    assert "private" not in read.stdout
    assert outside.read_text(encoding="utf-8") == "private"
    assert inside.exit_code == 0
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "data"


def test_bash_tool_does_not_pass_api_key_to_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": 'printf %s "$CAIRN_LLM_API_KEY"'}
        )
    )

    assert result.exit_code == 0
    assert result.stdout == ""
