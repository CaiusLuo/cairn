import asyncio
import codecs
import json
import os
import signal
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.workspace.workspace import Workspace

DEFAULT_STDOUT_CAPTURE_LIMIT = 64 * 1024
DEFAULT_STDERR_CAPTURE_LIMIT = 64 * 1024
PIPE_READ_CHUNK_SIZE = 16 * 1024

# Cairn-owned credentials that must never reach a child command.
CAIRN_SECRET_ENV_KEYS = frozenset({"CAIRN_LLM_API_KEY"})


def build_command_env(host_env: Mapping[str, str]) -> dict[str, str]:
    """Build the child-process environment from the host environment.

    Local tooling must behave exactly like the user's terminal, so the child
    inherits the full host environment (PATH, HOME, TMPDIR, VIRTUAL_ENV, LANG,
    LC_*, TERM, USER, SHELL, ...). Cairn-owned credentials are then stripped so
    they are never exposed to the command.
    """
    env = dict(host_env)
    for key in CAIRN_SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    captured = bytearray()
    truncated = False

    while True:
        chunk = await stream.read(PIPE_READ_CHUNK_SIZE)
        if not chunk:
            break

        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])

        if len(chunk) > remaining:
            truncated = True

    return bytes(captured), truncated


def _decode_output(data: bytes, truncated: bool) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=not truncated)


def _resolve_tmpdir(env: Mapping[str, str]) -> Path | None:
    """Resolve TMPDIR into a safe writable root for the macOS sandbox.

    Returns ``None`` unless TMPDIR is an absolute path to an existing directory
    that is not the filesystem root. A missing or malformed value can therefore
    only narrow the sandbox, never broaden it.
    """
    raw_tmpdir = env.get("TMPDIR")
    if not raw_tmpdir:
        return None

    candidate = Path(raw_tmpdir)
    if not candidate.is_absolute():
        return None

    try:
        resolved = candidate.resolve()
        is_directory = resolved.is_dir()
    except (OSError, RuntimeError):
        return None

    if not is_directory or resolved == Path(resolved.anchor):
        return None
    return resolved


def _macos_sandbox_profile(cwd: Path, writable_tmpdir: Path | None) -> str:
    writable_roots = [cwd]
    if writable_tmpdir is not None:
        writable_roots.append(writable_tmpdir)
    allowed = " ".join(f"(subpath {json.dumps(str(root))})" for root in writable_roots)
    return (
        "(version 1) (allow default) "
        f'(deny file-write*) (allow file-write* (literal "/dev/null") {allowed}) '
        "(deny network*)"
    )


class BashTool:
    name = "bash"
    description = (
        "Execute a shell command. Commands start at the workspace root; do not prepend "
        "`cd <workspace>`. Only change directory when intentionally entering a workspace "
        "subdirectory. Preserve command failure status when running verification commands."
    )

    def __init__(
        self,
        workspace: Workspace,
        timeout: float = 30.0,
        cleanup_timeout: float = 2.0,
        stdout_limit: int = DEFAULT_STDOUT_CAPTURE_LIMIT,
        stderr_limit: int = DEFAULT_STDERR_CAPTURE_LIMIT,
    ) -> None:
        if stdout_limit < 0 or stderr_limit < 0:
            raise ValueError("output capture limits must be non-negative")

        self.workspace = workspace
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }

    async def _collect_output(
        self, process: asyncio.subprocess.Process
    ) -> tuple[bytes, bool, bytes, bool, int]:
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, self.stdout_limit)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, self.stderr_limit)
        )

        (
            (stdout, stdout_truncated),
            (stderr, stderr_truncated),
            exit_code,
        ) = await asyncio.gather(stdout_task, stderr_task, process.wait())

        return stdout, stdout_truncated, stderr, stderr_truncated, exit_code

    async def _cleanup_process(self, process: asyncio.subprocess.Process) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=self.cleanup_timeout)

    async def _settle_collection(self, task: asyncio.Task[Any]) -> None:
        # execute() has already selected its primary result or exception.
        # Settling only prevents a leaked task and must not replace that outcome.
        try:
            if not task.done():
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.cleanup_timeout
                )
            await task
        except TimeoutError:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
        except Exception:
            pass

    async def execute(
        self,
        arguments: dict[str, Any],
    ) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if arguments.keys() - {"command"}:
            raise ValueError("bash only accepts the 'command' argument")

        cwd = self.workspace.root
        env = build_command_env(os.environ)
        command_argv = (
            [f"/bin/{command.strip()}"]
            if command.strip() in {"pwd", "ls"}
            else ["/bin/bash", "-o", "pipefail", "-c", command]
        )

        if sys.platform == "darwin":
            profile = _macos_sandbox_profile(cwd, _resolve_tmpdir(env))
            argv = ["/usr/bin/sandbox-exec", "-p", profile, *command_argv]
        elif sys.platform == "linux":
            bwrap = Path("/usr/bin/bwrap")
            if not bwrap.is_file():
                raise RuntimeError("BashTool requires bubblewrap on Linux")
            argv = [
                str(bwrap),
                "--die-with-parent",
                "--unshare-net",
                "--unshare-pid",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
            ]
            for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
                if Path(path).exists():
                    argv.extend(("--ro-bind", path, path))
            for parent in reversed(cwd.parents):
                if parent != Path("/"):
                    argv.extend(("--dir", str(parent)))
            argv.extend(("--bind", str(cwd), str(cwd), "--chdir", str(cwd)))
            argv.extend(command_argv)
        else:
            raise RuntimeError(f"BashTool has no sandbox for {sys.platform}")

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        # Keep draining the pipes while terminating the group: otherwise a full
        # pipe can prevent asyncio's process waiter from completing after exit.
        collection = asyncio.create_task(self._collect_output(process))
        try:
            (
                stdout,
                stdout_truncated,
                stderr,
                stderr_truncated,
                exit_code,
            ) = await asyncio.wait_for(asyncio.shield(collection), timeout=self.timeout)
        except TimeoutError:
            return ToolResult(
                stderr=f"Command '{command}' time out after {self.timeout}s",
                exit_code=-1,
            )
        else:
            return ToolResult(
                stdout=_decode_output(stdout, stdout_truncated),
                stderr=_decode_output(stderr, stderr_truncated),
                exit_code=exit_code,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        finally:
            try:
                await self._cleanup_process(process)
            finally:
                await self._settle_collection(collection)
