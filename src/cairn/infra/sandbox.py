"""Discardable execution isolation for evaluating model-generated code.

A temporary directory and a wall-clock timeout are **not** a sandbox: code that
the evaluated model wrote can read host secrets, write host files and reach the
network from inside a plain subprocess. This module runs a host-owned script (the
grader) together with the *generated code it imports* inside a bubblewrap
(``bwrap``) sandbox:

* read-only system mounts (``/usr``, ``/lib``, ``/lib64``, ``/bin``, ``/etc``)
  and nothing else of the host filesystem;
* the case workspace bind-mounted read-write at its own path, plus a private
  ``/tmp``;
* ``--unshare-all`` (private user/pid/mount/net/ipc/uts/cgroup namespaces), so a
  fresh ``/proc``, no network and no host session;
* a cleared environment with only ``PATH`` and ``HOME``;
* in-process resource limits (address space, CPU, open files, file size, no core)
  applied before the generated code runs.

The module is honest about the boundary it offers: it needs ``bwrap`` and working
user namespaces. When they are unavailable, ``available()`` is ``False`` and
callers must **block** any run that needs to execute generated code rather than
fall back to the host. It never broadens host permissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass

from cairn.core.models import ToolResult
from cairn.infra.workspace.workspace import Workspace

#: Default resource and time ceilings for one isolated behavior run.
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MEMORY_BYTES = 1024**3  # 1 GiB address space
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024

#: Directories bound read-only into the sandbox. Nothing else of the host is
#: visible, so the repository, tests, home and temporary markers stay hidden.
_RO_BINDS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc")


class IsolationUnavailable(RuntimeError):
    """Raised when the host cannot provide the required execution sandbox."""


@dataclass(frozen=True, slots=True)
class IsolationLimits:
    """Resource ceilings for one isolated run."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if not (0 < self.timeout_seconds <= 600):
            raise ValueError("timeout_seconds must be in (0, 600]")
        if not (16 * 1024 * 1024 <= self.memory_bytes <= 16 * 1024**3):
            raise ValueError("memory_bytes must be in [16 MiB, 16 GiB]")
        if not (512 <= self.max_output_bytes <= 1024**2):
            raise ValueError("max_output_bytes must be in [512 B, 1 MiB]")


def sandbox_interpreter() -> str:
    """A trusted system Python that lives inside the read-only ``/usr`` bind.

    The workspace's own interpreter may be a uv-managed build under ``/root`` or
    another host path that must stay hidden; behavior scripts only need the
    standard library, so a system interpreter is both sufficient and safer.
    """
    for candidate in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.11"):
        real = os.path.realpath(candidate)
        if (
            real.startswith("/usr/")
            and os.path.isfile(real)
            and os.access(real, os.X_OK)
        ):
            return real
    return ""


_probe_cache: bool | None = None


def _bwrap_path() -> str:
    return shutil.which("bwrap") or ""


def available() -> bool:
    """Whether a real sandbox can run here (bwrap present and namespaces work)."""
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    bwrap = _bwrap_path()
    interpreter = sandbox_interpreter()
    if not bwrap or not interpreter:
        _probe_cache = False
        return False
    argv: list[str] = [bwrap]
    for path in _RO_BINDS:
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--unshare-all",
        "--die-with-parent",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--",
        interpreter,
        "-c",
        "pass",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        _probe_cache = False
        return False
    _probe_cache = result.returncode == 0
    return _probe_cache


def _bootstrap(script: str, limits: IsolationLimits) -> str:
    """Set resource limits, then execute the host script in this interpreter."""
    cpu_seconds = int(limits.timeout_seconds) + 1
    return (
        "import resource as _r\n"
        f"_r.setrlimit(_r.RLIMIT_AS, ({limits.memory_bytes}, {limits.memory_bytes}))\n"
        f"_r.setrlimit(_r.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}))\n"
        "_r.setrlimit(_r.RLIMIT_NOFILE, (256, 256))\n"
        f"_r.setrlimit(_r.RLIMIT_FSIZE, ({64 * 1024 * 1024}, {64 * 1024 * 1024}))\n"
        "_r.setrlimit(_r.RLIMIT_CORE, (0, 0))\n"
        f"exec(compile({script!r}, '<behavior-check>', 'exec'))\n"
    )


def build_argv(workspace: Workspace, script: str, limits: IsolationLimits) -> list[str]:
    """The bubblewrap argv that runs ``script`` isolated over ``workspace``."""
    bwrap = _bwrap_path()
    interpreter = sandbox_interpreter()
    if not bwrap or not interpreter:
        raise IsolationUnavailable("bubblewrap or a sandbox interpreter is unavailable")
    root = str(workspace.root)
    argv: list[str] = [bwrap]
    for path in _RO_BINDS:
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        root,
        root,
        "--chdir",
        root,
        "--unshare-all",
        "--die-with-parent",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--new-session",
        "--",
        interpreter,
        "-B",
        "-c",
        _bootstrap(script, limits),
    ]
    return argv


class IsolatedRunner:
    """Runs host-owned scripts inside a fresh bubblewrap sandbox."""

    def __init__(self, limits: IsolationLimits | None = None) -> None:
        self.limits = limits or IsolationLimits()

    @staticmethod
    def available() -> bool:
        return available()

    async def run_script(self, workspace: Workspace, script: str) -> ToolResult:
        """Execute ``script`` isolated; raises ``IsolationUnavailable`` if blocked."""
        if not available():
            raise IsolationUnavailable(
                "no execution sandbox (bubblewrap + user namespaces) is available"
            )
        argv = build_argv(workspace, script, self.limits)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise IsolationUnavailable(
                f"could not start sandbox: {type(exc).__name__}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.limits.timeout_seconds
            )
        except TimeoutError:
            _kill_group(process)
            await _drain(process)
            return ToolResult(
                stdout="",
                stderr=f"behavior check exceeded {self.limits.timeout_seconds}s "
                "and was killed inside the sandbox",
                exit_code=-1,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        limit = self.limits.max_output_bytes
        out = stdout[:limit].decode("utf-8", "replace")
        err = stderr[:limit].decode("utf-8", "replace")
        return ToolResult(
            stdout=out,
            stderr=err,
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout_truncated=len(stdout) > limit,
            stderr_truncated=len(stderr) > limit,
        )


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), 9)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _drain(process: asyncio.subprocess.Process) -> None:
    """Reap a killed process so no zombie/reader task is left behind."""
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), timeout=5)


def require_isolation() -> None:
    """Raise ``IsolationUnavailable`` unless a sandbox can run here.

    Call this before any real eval that needs to execute generated code, so it is
    blocked up front instead of discovered halfway through a paid run.
    """
    if not available():
        raise IsolationUnavailable(
            "generated-code evaluation requires an execution sandbox "
            "(bubblewrap with working user namespaces); none is available. "
            "Refusing to run generated code on the host."
        )
