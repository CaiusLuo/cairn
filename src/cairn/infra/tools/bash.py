"""Execute shell commands on the host, from the Workspace root.

Cairn is local-first: a command runs as the invoking user, with the invoking
user's environment, in the Workspace directory. There is no OS sandbox, so this
module makes no isolation claim. It owns exactly the guarantees a local process
can keep:

* the child starts in the Workspace and inherits the host environment minus
  Cairn-owned credentials;
* stdout/stderr are captured with a fixed byte ceiling and a fixed timeout;
* the whole process group is killed on timeout or cancellation, and the group is
  always reaped before ``execute`` returns;
* a cancelled or failed command still reports what it observed.

Authority is not granted here. Whether a shell may run at all is decided before
``execute`` by the approval path in ``core.permissions``; a local shell is a
host-level capability, so it is asked for explicitly once per session.
"""

import asyncio
import codecs
import os
import signal
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import (
    InvalidArguments,
    ToolExecutionContext,
    WorkspaceOnlyTool,
)
from cairn.infra.tools.capture import read_bounded
from cairn.infra.tools.credential_env import SECRET_ENV_KEYS
from cairn.infra.workspace.workspace import Workspace

DEFAULT_STDOUT_CAPTURE_LIMIT = 64 * 1024
DEFAULT_STDERR_CAPTURE_LIMIT = 64 * 1024

#: Commands a caller may name directly instead of going through the shell.
_DIRECT_COMMANDS = frozenset({"pwd", "ls"})


def build_command_env(
    host_env: Mapping[str, str],
    secret_env_keys: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Build the child-process environment from the host environment.

    Local tooling must behave exactly like the user's terminal, so the child
    inherits the full host environment (PATH, HOME, TMPDIR, VIRTUAL_ENV, LANG,
    LC_*, TERM, USER, SHELL, ...). Cairn-owned credentials are then stripped so
    they are never exposed to the command.
    """
    env = dict(host_env)
    for key in SECRET_ENV_KEYS | secret_env_keys:
        env.pop(key, None)
    return env


def _decode_output(data: bytes, truncated: bool) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=not truncated)


def _command_argv(command: str) -> list[str]:
    """The argv that runs ``command`` through the host shell.

    POSIX hosts use ``bash -o pipefail`` so a failing stage still fails the
    pipeline, matching what the model was previously told. A host without that
    shell runs the command through the platform default, which keeps the tool
    usable off POSIX at the cost of ``pipefail``.
    """
    stripped = command.strip()
    if stripped in _DIRECT_COMMANDS and Path(f"/bin/{stripped}").is_file():
        return [f"/bin/{stripped}"]
    if sys.platform != "win32" and Path("/bin/bash").is_file():
        return ["/bin/bash", "-o", "pipefail", "-c", command]
    return [command]


def workspace_python(workspace_root: Path | None = None) -> str:
    """The interpreter a command in this workspace should run, by priority.

    The workspace's own ``.venv`` wins: ``uv`` and the project's instructions
    create it, so it is what the project means by "Python". A workspace without
    a venv falls back to the interpreter running Cairn. Returns an empty string
    when neither exists; the caller decides whether that is fatal.
    """
    if workspace_root is not None:
        for relative in (
            Path(".venv") / "bin" / "python",
            Path(".venv") / "bin" / "python3",
            Path(".venv") / "Scripts" / "python.exe",
        ):
            candidate = workspace_root / relative
            if candidate.is_file():
                return str(candidate)
    return sys.executable if sys.executable else ""


class BashTool(WorkspaceOnlyTool):
    name = "bash"
    description = (
        "Execute a shell command on this machine. Commands start at the workspace "
        "root; do not prepend `cd <workspace>`. Only change directory when "
        "intentionally entering a workspace subdirectory. Preserve command failure "
        "status when running verification commands. The command runs as you, with "
        "your environment, and is not sandboxed: it can reach anything you can, "
        "including the network and files outside the workspace. It is executed only "
        "after the session is trusted with local shell access."
    )

    def __init__(
        self,
        workspace: Workspace,
        timeout: float = 30.0,
        cleanup_timeout: float = 2.0,
        stdout_limit: int = DEFAULT_STDOUT_CAPTURE_LIMIT,
        stderr_limit: int = DEFAULT_STDERR_CAPTURE_LIMIT,
        *,
        secret_env_keys: frozenset[str] = frozenset(),
    ) -> None:
        if stdout_limit < 0 or stderr_limit < 0:
            raise ValueError("output capture limits must be non-negative")

        self.workspace = workspace
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.secret_env_keys = secret_env_keys

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
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        """Validate the Bash tool input contract.

        Argument validity is owned by the tool that declares the schema, so a
        malformed call is an ``InvalidArguments`` tool failure and never an
        authorization decision. Shell syntax is not validated here: an unterminated
        quote is the shell's error to report.
        """
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise InvalidArguments("command must be a non-empty string without NUL")

        unexpected = arguments.keys() - {"command"}
        if unexpected:
            raise InvalidArguments(
                f"unexpected bash command argument: {sorted(unexpected)}"
            )

    async def _collect_output(
        self, process: asyncio.subprocess.Process
    ) -> tuple[bytes, bool, bytes, bool, int]:
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_task = asyncio.create_task(
            read_bounded(process.stdout, self.stdout_limit)
        )
        stderr_task = asyncio.create_task(
            read_bounded(process.stderr, self.stderr_limit)
        )

        (
            (stdout, stdout_truncated),
            (stderr, stderr_truncated),
            exit_code,
        ) = await asyncio.gather(stdout_task, stderr_task, process.wait())

        return stdout, stdout_truncated, stderr, stderr_truncated, exit_code

    async def _cleanup_process(self, process: asyncio.subprocess.Process) -> None:
        """Kill the whole process group, then reap it within a fixed budget.

        A shell command can leave descendants behind. Signalling the group is what
        stops them; waiting bounded is what keeps a stuck child from hanging the
        turn.
        """
        with suppress(ProcessLookupError, PermissionError, OSError):
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
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        command = arguments["command"]

        cwd = self.workspace.root
        env = build_command_env(os.environ, self.secret_env_keys)
        argv = _command_argv(command)

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


def _resolve_writable_root(raw_path: str | None) -> Path | None:
    """Resolve a candidate root, rejecting values that could only widen access.

    Returns ``None`` for an empty, relative, nonexistent or filesystem-root
    value, so a malformed setting can never broaden what it describes.
    """
    if not raw_path or not raw_path.strip():
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == Path(resolved.anchor):
        return None
    return resolved


def resolve_tmpdir(env: Mapping[str, str]) -> Path | None:
    """The effective TMPDIR, when it names a real directory."""
    raw = env.get("TMPDIR")
    resolved = _resolve_writable_root(raw)
    return resolved if resolved is not None and resolved.is_dir() else None


def resolve_uv_cache_dir(env: Mapping[str, str]) -> Path | None:
    """uv's effective cache root, mirroring uv's own lookup order."""
    explicit = env.get("UV_CACHE_DIR")
    if explicit is not None:
        return _resolve_writable_root(explicit) if explicit.strip() else None
    xdg = env.get("XDG_CACHE_HOME")
    if xdg and Path(xdg).is_absolute():
        return _resolve_writable_root(str(Path(xdg) / "uv"))
    home = env.get("HOME")
    if home and home.strip():
        return _resolve_writable_root(str(Path(home) / ".cache" / "uv"))
    return None
