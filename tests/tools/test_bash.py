import asyncio
import os
import shlex
import shutil
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.tools.base import InvalidArguments
from cairn.tools.bash import (
    PIPE_READ_CHUNK_SIZE,
    BashTool,
    _decode_output,
    _read_bounded,
    _resolve_tmpdir,
    _resolve_uv_cache_dir,
    build_command_env,
)
from cairn.workspace.workspace import Workspace

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
    description = schema["function"]["description"].lower()

    assert schema["function"]["name"] == "bash"
    assert schema["function"]["parameters"]["required"] == ["command"]
    network_access = schema["function"]["parameters"]["properties"]["network_access"]
    assert network_access["type"] == "boolean"
    assert network_access["default"] is False
    justification = schema["function"]["parameters"]["properties"]["justification"]
    assert justification["type"] == "string"
    assert "network_access is true" in justification["description"]
    assert schema["function"]["parameters"]["additionalProperties"] is False
    assert "workspace root" in description
    assert "do not prepend `cd <workspace>`" in description
    assert "workspace subdirectory" in description
    assert "failure status" in description


def test_build_command_env_inherits_host_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/host/path")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("TMPDIR", "/host/tmp")
    monkeypatch.setenv("VIRTUAL_ENV", "/host/.venv")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("MY_TOOL_VAR", "arbitrary-value")

    env = build_command_env(os.environ)

    assert env["PATH"] == "/host/path"
    assert env["HOME"] == "/host/home"
    assert env["TMPDIR"] == "/host/tmp"
    assert env["VIRTUAL_ENV"] == "/host/.venv"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["MY_TOOL_VAR"] == "arbitrary-value"


def test_build_command_env_removes_cairn_secret_and_preserves_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")
    monkeypatch.setenv("KEEP_ME", "kept")

    env = build_command_env(os.environ)

    assert "CAIRN_LLM_API_KEY" not in env
    assert env["KEEP_ME"] == "kept"


def test_build_command_env_does_not_mutate_host_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")

    host_env = dict(os.environ)
    env = build_command_env(host_env)

    assert "CAIRN_LLM_API_KEY" not in env
    assert "CAIRN_LLM_API_KEY" in host_env


@pytest.mark.parametrize("tmpdir", [None, "", "relative/tmp", "/", "//"])
def test_resolve_tmpdir_rejects_unsafe_values(tmpdir: str | None) -> None:
    env = {} if tmpdir is None else {"TMPDIR": tmpdir}

    assert _resolve_tmpdir(env) is None


def test_resolve_tmpdir_rejects_missing_directory(tmp_path: Path) -> None:
    assert _resolve_tmpdir({"TMPDIR": str(tmp_path / "missing")}) is None


def test_resolve_tmpdir_rejects_non_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("data", encoding="utf-8")

    assert _resolve_tmpdir({"TMPDIR": str(file_path)}) is None


def test_resolve_tmpdir_returns_resolved_absolute_directory(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    resolved = _resolve_tmpdir({"TMPDIR": str(tmpdir)})

    assert resolved == tmpdir.resolve()
    assert resolved is not None
    assert resolved.is_absolute()


def test_resolve_tmpdir_resolves_symlinks(tmp_path: Path) -> None:
    real_tmpdir = tmp_path / "real-tmp"
    real_tmpdir.mkdir()
    linked_tmpdir = tmp_path / "linked-tmp"
    linked_tmpdir.symlink_to(real_tmpdir, target_is_directory=True)

    assert _resolve_tmpdir({"TMPDIR": str(linked_tmpdir)}) == real_tmpdir.resolve()


def test_resolve_uv_cache_dir_prefers_explicit_uv_cache_dir(tmp_path: Path) -> None:
    env = {
        "UV_CACHE_DIR": str(tmp_path / "explicit"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
    }

    assert _resolve_uv_cache_dir(env) == (tmp_path / "explicit").resolve()


def test_resolve_uv_cache_dir_falls_back_to_xdg_cache_home(tmp_path: Path) -> None:
    env = {
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
    }

    assert _resolve_uv_cache_dir(env) == (tmp_path / "xdg" / "uv").resolve()


def test_resolve_uv_cache_dir_falls_back_to_home_dot_cache_uv(tmp_path: Path) -> None:
    assert (
        _resolve_uv_cache_dir({"HOME": str(tmp_path / "home")})
        == (tmp_path / "home" / ".cache" / "uv").resolve()
    )


def test_resolve_uv_cache_dir_ignores_unusable_xdg_cache_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    expected = (home / ".cache" / "uv").resolve()

    assert _resolve_uv_cache_dir({"XDG_CACHE_HOME": "", "HOME": str(home)}) == expected
    assert (
        _resolve_uv_cache_dir({"XDG_CACHE_HOME": "relative-xdg", "HOME": str(home)})
        == expected
    )


@pytest.mark.parametrize("cache_dir", ["", "   ", "relative/cache", "/", "//"])
def test_resolve_uv_cache_dir_rejects_unsafe_explicit_values(cache_dir: str) -> None:
    assert _resolve_uv_cache_dir({"UV_CACHE_DIR": cache_dir}) is None


def test_resolve_uv_cache_dir_fails_closed_on_empty_explicit_value(
    tmp_path: Path,
) -> None:
    env = {
        "UV_CACHE_DIR": "",
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
    }

    assert _resolve_uv_cache_dir(env) is None


def test_resolve_uv_cache_dir_allows_missing_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "missing" / "uv"
    assert not cache_dir.exists()

    assert (
        _resolve_uv_cache_dir({"UV_CACHE_DIR": str(cache_dir)}) == cache_dir.resolve()
    )


def test_resolve_uv_cache_dir_resolves_symlinks(tmp_path: Path) -> None:
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    linked_cache = tmp_path / "linked-cache"
    linked_cache.symlink_to(real_cache, target_is_directory=True)

    assert (
        _resolve_uv_cache_dir({"UV_CACHE_DIR": str(linked_cache)})
        == real_cache.resolve()
    )


def test_bash_tool_child_inherits_host_env_and_keeps_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("TMPDIR", "/host/tmp")
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")

    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {
                "command": (
                    'printf "%s|%s|%s|%s" "$PWD" "$HOME" "$TMPDIR" '
                    '"${CAIRN_LLM_API_KEY:-}"'
                )
            }
        )
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{tmp_path}|/host/home|/host/tmp|"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_runs_workspace_uv_managed_python(tmp_path: Path) -> None:
    home = Path.home().resolve()
    runtime_root = Path(sys.base_prefix).resolve()
    runtime_alias = Path(sys.base_exec_prefix)
    if (
        not runtime_root.is_relative_to(home)
        or not runtime_alias.is_relative_to(home)
        or not runtime_alias.is_symlink()
        or runtime_alias.resolve() != runtime_root
    ):
        pytest.skip("current Python runtime is not managed under the user home")

    runtime = runtime_alias / "bin" / Path(sys.executable).resolve().name
    if not runtime.is_file():
        pytest.skip("current Python runtime has no executable under its uv alias")

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(runtime)

    result = asyncio.run(
        BashTool(Workspace(tmp_path)).execute(
            {
                "command": (
                    ".venv/bin/python -c 'import ssl; print(\"sandbox-python-ok\")'"
                )
            }
        )
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "sandbox-python-ok\n"
    assert result.stderr == ""


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_denies_local_network_bind(tmp_path: Path) -> None:
    python = tmp_path / "python"
    python.symlink_to(Path(sys.executable).resolve())
    script = (
        "import socket\n"
        'print("python-started", flush=True)\n'
        "with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:\n"
        '    sock.bind(("127.0.0.1", 0))\n'
    )

    result = asyncio.run(
        BashTool(Workspace(tmp_path)).execute(
            {"command": f"{shlex.quote(str(python))} -c {shlex.quote(script)}"}
        )
    )

    assert result.exit_code != 0
    assert result.stdout == "python-started\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_allows_writes_to_workspace_and_host_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    host_tmpdir = tmp_path / "host-tmp"
    host_tmpdir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(host_tmpdir))
    tool = BashTool(Workspace(workspace_root))
    outside_path = shlex.quote(str(outside))
    tmpdir_path = shlex.quote(str(host_tmpdir / "scratch.txt"))

    read = asyncio.run(tool.execute({"command": f"cat {outside_path}"}))
    outside_write = asyncio.run(
        tool.execute({"command": f"printf data > {outside_path}"})
    )
    inside_write = asyncio.run(tool.execute({"command": "printf data > inside.txt"}))
    tmpdir_write = asyncio.run(
        tool.execute({"command": f"printf data > {tmpdir_path}"})
    )

    assert inside_write.exit_code == 0, inside_write.stderr
    assert (workspace_root / "inside.txt").read_text(encoding="utf-8") == "data"
    assert tmpdir_write.exit_code == 0, tmpdir_write.stderr
    assert (host_tmpdir / "scratch.txt").read_text(encoding="utf-8") == "data"
    assert outside_write.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "private"
    assert read.exit_code == 0, read.stderr
    assert read.stdout == "private"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
@pytest.mark.parametrize("cache_exists", [True, False], ids=["exists", "missing"])
def test_macos_sandbox_allows_writes_to_effective_uv_cache_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_exists: bool,
) -> None:
    host_tmpdir = tmp_path / "host-tmp"
    host_tmpdir.mkdir()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    cache_dir = tmp_path / "uv-cache"
    if cache_exists:
        cache_dir.mkdir()
    monkeypatch.setenv("TMPDIR", str(host_tmpdir))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache_dir))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    result = asyncio.run(
        BashTool(Workspace(workspace_root)).execute(
            {
                "command": (
                    'mkdir -p "$UV_CACHE_DIR/sdists-v9" && '
                    'printf cached > "$UV_CACHE_DIR/sdists-v9/entry"'
                )
            }
        )
    )

    assert result.exit_code == 0, result.stderr
    assert (cache_dir / "sdists-v9" / "entry").read_text(encoding="utf-8") == "cached"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_denies_home_writes_outside_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    other_cache_file = home / ".cache" / "cairn-sandbox-denied" / "entry"
    home_file = home / "cairn-sandbox-denied.txt"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    tool = BashTool(Workspace(workspace_root))
    other_cache_path = shlex.quote(str(other_cache_file))
    home_path = shlex.quote(str(home_file))

    try:
        other_cache_write = asyncio.run(
            tool.execute({"command": f"printf data > {other_cache_path}"})
        )
        home_write = asyncio.run(
            tool.execute({"command": f"printf data > {home_path}"})
        )
    finally:
        shutil.rmtree(home / ".cache" / "cairn-sandbox-denied", ignore_errors=True)
        home_file.unlink(missing_ok=True)

    assert other_cache_write.exit_code != 0
    assert home_write.exit_code != 0
    assert not other_cache_file.exists()
    assert not home_file.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_runs_uv_with_real_host_environment(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    # Deliberately no UV_CACHE_DIR/XDG_CACHE_HOME/HOME override: this must work
    # with the user's real uv cache under the default host environment.
    result = asyncio.run(
        BashTool(Workspace(workspace_root), timeout=90.0).execute(
            {"command": "uv run --no-sync python --version"}
        )
    )

    assert result.exit_code == 0, result.stderr
    assert "Python" in result.stdout
    assert list(workspace_root.glob("uv-*.lock")) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_macos_sandbox_runs_uv_pytest_with_real_host_environment() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    repo_root = Path(__file__).resolve().parents[2]
    if not (repo_root / "tests" / "core" / "test_agent.py").is_file():
        pytest.skip("repository layout is not available")

    result = asyncio.run(
        BashTool(Workspace(repo_root), timeout=180.0).execute(
            {"command": "uv run --no-sync pytest tests/core/test_agent.py -v"}
        )
    )

    assert result.exit_code == 0, result.stderr
    assert "3 passed" in result.stdout


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

    with pytest.raises(InvalidArguments, match="command"):
        asyncio.run(BashTool(workspace=Workspace(tmp_path)).execute(arguments))

    create_process.assert_not_called()


def test_bash_tool_leaves_shell_syntax_to_the_shell(tmp_path: Path) -> None:
    """Argument validity is the tool's contract; shell syntax is the shell's.

    An unterminated quote must reach the shell and come back as a normal command
    failure, not as a fabricated permission denial.
    """
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": "printf 'unterminated"}
        )
    )

    assert result.exit_code != 0
    assert result.stderr != ""


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
    process.stdout = _completed_stream(stdout)
    process.stderr = _completed_stream(stderr)
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
    assert create_process.call_args.args[-5:] == (
        "/bin/bash",
        "-o",
        "pipefail",
        "-c",
        command,
    )
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert result.exit_code == exit_code


def test_bash_tool_cancellation_kills_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        collection_started = asyncio.Event()

        process = AsyncMock()
        process.pid = 12345
        process.returncode = None

        async def block_read(_limit: int) -> bytes:
            collection_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process.stdout.read.side_effect = block_read
        process.stderr.read.side_effect = block_read
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

        await collection_started.wait()

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        killpg.assert_called_once_with(
            12345,
            signal.SIGKILL,
        )

        assert process.wait.await_count == 2
        process.stdout.read.assert_awaited_once()
        process.stderr.read.assert_awaited_once()

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

        async def block_read(_limit: int) -> bytes:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process.stdout.read.side_effect = block_read
        process.stderr.read.side_effect = block_read
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

        assert process.wait.await_count == 2
        process.stdout.read.assert_awaited_once()
        process.stderr.read.assert_awaited_once()

    asyncio.run(scenario())


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
        monkeypatch.setattr("cairn.tools.bash.sys.platform", "darwin")
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


def test_bare_ls_uses_system_binary(
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


def test_bash_pipeline_preserves_upstream_failure_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path)).execute(
            {"command": "/bin/sh -c 'printf payload; exit 7' | tail"}
        )
    )

    assert result.exit_code == 7
    assert result.stdout == "payload"
    assert result.stderr == ""


def test_bash_tool_kills_timed_out_process(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(workspace=Workspace(tmp_path), timeout=0.01).execute(
            {"command": "sleep 1"}
        )
    )

    assert result.exit_code == -1
    assert "time out after 0.01s" in result.stderr


def test_bash_tool_allows_outside_reads_and_confines_writes_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writable roots differ per sandbox: macOS confines writes to the
    # workspace plus TMPDIR, while Linux bubblewrap also exposes a private /tmp
    # tmpfs and only reaches the host through read-only system binds. Keep the
    # effective TMPDIR narrow on macOS and probe a host path that bubblewrap does
    # not mount on Linux.
    host_tmpdir = tmp_path / "host-tmp"
    host_tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(host_tmpdir))
    if sys.platform == "linux":
        readable = Path("/etc/os-release")
        unwritable = Path.home() / ".cairn-sandbox-outside-probe"
    else:
        readable = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        readable.write_text("private", encoding="utf-8")
        unwritable = readable
    tool = BashTool(workspace=Workspace(tmp_path))
    readable_path = shlex.quote(str(readable))
    unwritable_path = shlex.quote(str(unwritable))
    before = unwritable.read_text(encoding="utf-8") if unwritable.exists() else None

    read = asyncio.run(tool.execute({"command": f"cat {readable_path}"}))
    outside_write = asyncio.run(
        tool.execute({"command": f"printf data > {unwritable_path}"})
    )
    inside = asyncio.run(tool.execute({"command": "printf data > inside.txt"}))

    assert read.exit_code == 0, read.stderr
    assert read.stdout == readable.read_text(encoding="utf-8")
    assert outside_write.exit_code != 0
    after = unwritable.read_text(encoding="utf-8") if unwritable.exists() else None
    assert after == before
    assert inside.exit_code == 0, inside.stderr
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


@pytest.mark.parametrize(
    ("write_stdout", "write_stderr"),
    [
        (True, False),
        (False, True),
        (True, True),
    ],
    ids=("stdout-only", "stderr-only", "stdout-and-stderr"),
)
def test_bash_drains_large_output_without_deadlock(
    tmp_path: Path,
    write_stdout: bool,
    write_stderr: bool,
) -> None:
    writes: list[str] = []
    if write_stdout:
        writes.append(f"printf '%{_LARGE_OUTPUT_CHUNK_BYTES}s' x")
    if write_stderr:
        writes.append(f"printf '%{_LARGE_OUTPUT_CHUNK_BYTES}s' y >&2")
    command = (
        f'i=0; while [ "$i" -lt {_LARGE_OUTPUT_CHUNKS} ]; do '
        + "; ".join(writes)
        + "; i=$((i + 1)); done"
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
    assert result.stdout == (" " * stdout_limit if write_stdout else "")
    assert result.stderr == (" " * stderr_limit if write_stderr else "")
    assert result.stdout_truncated is write_stdout
    assert result.stderr_truncated is write_stderr


def test_bash_does_not_mark_small_output_truncated(
    tmp_path: Path,
) -> None:
    result = asyncio.run(
        BashTool(
            Workspace(tmp_path),
            stdout_limit=128,
            stderr_limit=128,
        ).execute({"command": "printf hello; printf error >&2"})
    )

    assert result.stdout == "hello"
    assert result.stderr == "error"
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_bounded_output_does_not_emit_partial_utf8_character() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()

        reader.feed_data("你你".encode())
        reader.feed_eof()

        data, truncated = await _read_bounded(reader, 4)

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

        data, truncated = await _read_bounded(stream, 4)

        assert data == b"aaaa"
        assert truncated is True
        assert stream.read.await_count == 3
        assert all(
            read.args == (PIPE_READ_CHUNK_SIZE,) for read in stream.read.await_args_list
        )

    asyncio.run(scenario())
