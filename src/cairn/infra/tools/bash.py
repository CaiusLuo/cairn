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
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.tools.capture import read_bounded
from cairn.infra.tools.credential_env import SECRET_ENV_KEYS
from cairn.infra.tools.sandbox import resolve_tmpdir, resolve_uv_cache_dir
from cairn.infra.workspace.workspace import Workspace

DEFAULT_STDOUT_CAPTURE_LIMIT = 64 * 1024
DEFAULT_STDERR_CAPTURE_LIMIT = 64 * 1024


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


def _git_metadata_paths(cwd: Path) -> tuple[Path, ...]:
    """Git metadata a sandboxed command must never be able to write.

    Two shapes exist. A normal checkout has a ``.git`` directory inside the
    workspace. A linked worktree has a ``.git`` file holding ``gitdir: <path>``
    that names an admin directory which usually lives outside the workspace.

    The mount list is also a *read* grant on Linux: Bubblewrap has already hidden
    every host path outside the workspace and the system directories, so mounting
    a candidate makes it readable. ``cwd/.git`` is writable by the command, which
    makes its contents untrusted input, so an external candidate is only mounted
    when Git's own layout confirms the admin directory belongs to this workspace.

    Only paths inside the workspace, or externally verified ones, are returned.
    A symlinked ``.git`` is not followed at all, because the mount source would
    resolve outside the workspace. Absent paths are skipped.
    """
    git_path = cwd / ".git"

    if git_path.is_symlink():
        # Following this would mount whatever the link names, outside the
        # workspace and outside any verification. Leave it to the workspace bind.
        return ()

    if git_path.is_dir():
        return (git_path,)

    if not git_path.is_file():
        return ()

    candidates: list[Path] = [git_path]
    gitdir = _verified_worktree_gitdir(git_path)
    if gitdir is not None:
        candidates.append(gitdir)
        # The admin directory's ``commondir`` names the shared main Git
        # directory (objects, refs). Read-only Git commands need it, so it is
        # mounted too, once it is confirmed to be that admin directory's parent.
        common = _verified_common_dir(gitdir)
        if common is not None:
            candidates.append(common)

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in seen and candidate.exists():
            seen.add(candidate)
            unique.append(candidate)
    return tuple(unique)


def _verified_worktree_gitdir(gitfile: Path) -> Path | None:
    """Return a worktree admin directory only when Git's own layout confirms it.

    Two independent facts are required, because either alone is forgeable:

    * ``<admin>/gitdir`` names this workspace's ``.git`` file. A forged pointer
      cannot satisfy that unless the external directory already holds a matching
      file, which only a registered worktree of this workspace does.
    * the admin directory sits at ``<common>/worktrees/<name>``. Requiring the
      documented layout stops an in-workspace directory from presenting itself as
      an admin directory and steering ``commondir`` at an ancestor.

    A symlinked admin directory is refused: the mount source would resolve
    somewhere the checks did not cover.
    """
    gitdir = _read_gitdir_pointer(gitfile)
    if gitdir is None or gitdir.is_symlink() or not gitdir.is_dir():
        return None
    if gitdir.parent.name != "worktrees":
        return None

    back_pointer = gitdir / "gitdir"
    try:
        raw = back_pointer.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    recorded = Path(raw)
    if not recorded.is_absolute():
        recorded = gitdir / recorded
    try:
        if recorded.resolve() != gitfile.resolve():
            return None
    except OSError:
        return None
    return gitdir


def _verified_common_dir(gitdir: Path) -> Path | None:
    """Return the shared Git directory only when it is exactly the admin parent.

    ``commondir`` is a second untrusted pointer read from the same external
    directory. Git places the admin directory directly under ``<common>/worktrees``,
    so the resolved value must equal the admin directory's grandparent; accepting
    only that value keeps the pointer from redirecting a mount at an unrelated
    host path while still mounting the shared object store Git needs.
    """
    common = _read_common_dir(gitdir)
    if common is None or common.is_symlink() or not common.is_dir():
        return None
    try:
        if common.resolve() != gitdir.parent.parent.resolve():
            return None
    except (OSError, ValueError):
        return None
    return common


def _read_gitdir_pointer(gitfile: Path) -> Path | None:
    """Resolve the ``gitdir:`` target of a linked worktree's ``.git`` file."""
    try:
        raw = gitfile.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = "gitdir:"
    if not raw.startswith(prefix):
        return None
    target = Path(raw[len(prefix) :].strip())
    if not target.is_absolute():
        # Relative pointers are resolved against the directory holding the file.
        target = (gitfile.parent / target).resolve()
    return target


def _read_common_dir(gitdir: Path) -> Path | None:
    """Resolve the shared main Git directory a worktree admin directory names.

    ``commondir`` sits inside the worktree's own gitdir and usually holds a
    relative path such as ``../..`` pointing at the main ``.git``.
    """
    pointer = gitdir / "commondir"
    try:
        raw = pointer.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = (gitdir / target).resolve()
    return target


def _macos_sandbox_profile(
    cwd: Path,
    writable_tmpdir: Path | None,
    writable_uv_cache: Path | None,
    network_access: bool = False,
) -> str:
    writable_roots = [cwd]
    for root in (writable_tmpdir, writable_uv_cache):
        if root is not None:
            writable_roots.append(root)
    allowed = " ".join(f"(subpath {json.dumps(str(root))})" for root in writable_roots)
    # The workspace-wide allow above covers Git metadata inside it. sandbox-exec
    # evaluates rules in order, so a later deny narrows the earlier allow and
    # keeps the metadata read-only while ordinary source files stay writable.
    denied = " ".join(
        f"(subpath {json.dumps(str(path))})" for path in _git_metadata_paths(cwd)
    )
    deny_metadata = f"(deny file-write* {denied}) " if denied else ""
    return (
        "(version 1) (allow default) "
        f'(deny file-write*) (allow file-write* (literal "/dev/null") {allowed}) '
        + deny_metadata
        + ("" if network_access else "(deny network*)")
    )


class BashTool:
    name = "bash"
    description = (
        "Execute a shell command. Commands start at the workspace root; do not prepend "
        "`cd <workspace>`. Only change directory when intentionally entering a workspace "
        "subdirectory. Preserve command failure status when running verification commands. "
        "Network is denied by default. Set network_access=true only when the command "
        "requires network and provide a concise justification; do not request network "
        "preemptively for local commands."
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
                        "network_access": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Request the NETWORK capability for this command. "
                                "Denied by default; requires user approval and a "
                                "justification."
                            ),
                        },
                        "justification": {
                            "type": "string",
                            "description": (
                                "Why this command needs network access. Required "
                                "when network_access is true; shown to the user."
                            ),
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

        unexpected = arguments.keys() - {"command", "network_access", "justification"}
        if unexpected:
            raise InvalidArguments(
                f"unexpected bash command argument: {sorted(unexpected)}"
            )

        network_access = arguments.get("network_access", False)
        if not isinstance(network_access, bool):
            raise InvalidArguments("network_access must be a boolean")

        justification = arguments.get("justification", "")
        if not isinstance(justification, str):
            raise InvalidArguments("justification must be a string")

        if network_access and not justification.strip():
            raise InvalidArguments("network_access requires a non-empty justification")

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
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        command = arguments["command"]
        network_access = context is not None and context.network_access

        cwd = self.workspace.root
        env = build_command_env(os.environ, self.secret_env_keys)
        command_argv = (
            [f"/bin/{command.strip()}"]
            if command.strip() in {"pwd", "ls"}
            else ["/bin/bash", "-o", "pipefail", "-c", command]
        )

        if sys.platform == "darwin":
            profile = _macos_sandbox_profile(
                cwd,
                resolve_tmpdir(env),
                resolve_uv_cache_dir(env),
                network_access=network_access,
            )
            argv = ["/usr/bin/sandbox-exec", "-p", profile, *command_argv]
        elif sys.platform == "linux":
            bwrap = Path("/usr/bin/bwrap")
            if not bwrap.is_file():
                raise RuntimeError("BashTool requires bubblewrap on Linux")
            argv = [
                str(bwrap),
                "--die-with-parent",
                *([] if network_access else ["--unshare-net"]),
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
            # The workspace bind above is writable, so the Git metadata inside it
            # would be writable too. Re-mount each metadata path read-only after
            # the writable bind: bubblewrap applies mounts in order, so the later
            # read-only mount shadows the earlier writable one.
            for metadata in _git_metadata_paths(cwd):
                argv.extend(("--ro-bind", str(metadata), str(metadata)))
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
