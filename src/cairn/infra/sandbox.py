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

This module is also the backend migration boundary: callers use only
``available()``, ``build_argv()``, ``build_shell_argv()`` and ``IsolatedRunner``.
Replacing bubblewrap must preserve those contracts, keep host capability probes
inside this module, and fail closed with ``IsolationUnavailable``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.credential_paths import is_credential_path
from cairn.infra.workspace.workspace import Workspace

#: Default resource and time ceilings for one isolated behavior run.
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MEMORY_BYTES = 1024**3  # 1 GiB address space
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024

#: Directories bound read-only into the sandbox. This is deliberately the *code
#: and libraries only*: read-only is not confidential, so ``/etc`` and ``/sbin``
#: are NOT mounted — ``/etc`` alone exposes ``/etc/passwd``, ``/etc/shadow`` and
#: any host secret/config file to the generated code. With this set the
#: repository, tests, home, ``/root`` and ``/etc`` stay entirely hidden; the
#: wrapped interpreter only needs the standard library under ``/usr``.
_RO_BINDS = ("/usr", "/lib", "/lib64", "/bin")


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


#: Feature support per resolved bwrap path, so `--help` is not run per command.
_userns_lock_cache: dict[str, bool] = {}


def _bwrap_supports_userns_lock(bwrap: str) -> bool:
    """Whether this bwrap can forbid nested user namespaces.

    Ubuntu 22.04 still ships bubblewrap 0.6.1, which rejects the newer
    ``--disable-userns`` / ``--assert-userns-disabled`` options at argv parsing
    time. Feature detection keeps the stronger contract on newer hosts while
    letting the older sandbox run with its existing unshare/capability boundary.
    """
    cached = _userns_lock_cache.get(bwrap)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [bwrap, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationUnavailable(
            f"cannot determine {bwrap} user-namespace capabilities: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    help_text = f"{result.stdout or ''}{result.stderr or ''}"
    if not help_text.strip():
        raise IsolationUnavailable(
            f"cannot determine {bwrap} user-namespace capabilities: "
            "empty bwrap --help output"
        )
    supported = (
        "--disable-userns" in help_text and "--assert-userns-disabled" in help_text
    )
    _userns_lock_cache[bwrap] = supported
    return supported


# Minimal cBPF/seccomp fallback for bwrap 0.6.x, which lacks --disable-userns.
_BPF_LD_W_ABS = 0x20
_BPF_JEQ = 0x15
_BPF_JSET = 0x45
_BPF_RET_K = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_CLONE_NEWUSER = 0x10000000
_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_AARCH64 = 0xC00000B7
_NR_UNSHARE = 272
_NR_CLONE3 = 435


def _userns_seccomp_program() -> bytes:
    """Deny nested user namespace creation on old bubblewrap hosts."""
    machine = os.uname().machine.lower()
    if machine in {"x86_64", "amd64"}:
        arch, clone_nr = _AUDIT_ARCH_X86_64, 56
    elif machine in {"aarch64", "arm64"}:
        arch, clone_nr = _AUDIT_ARCH_AARCH64, 220
    else:
        raise IsolationUnavailable(
            f"seccomp userns fallback is unsupported on {machine}"
        )
    deny = _SECCOMP_RET_ERRNO | errno.EPERM
    enosys = _SECCOMP_RET_ERRNO | errno.ENOSYS
    allow = _SECCOMP_RET_ALLOW

    # Instruction indices are fixed so every jump is forward and bounded.
    nr_ld, clone_jmp, args_ld, clone3_jmp = 2, 4, 5, 7
    clone3_ret, deny_ret, allow_ret, deny_all_ret = 8, 9, 10, 11
    instructions: list[tuple[int, int, int, int]] = []

    def stmt(code: int, k: int) -> None:
        instructions.append((code, 0, 0, k))

    def jump(code: int, k: int, true_index: int, false_index: int) -> None:
        index = len(instructions)
        instructions.append((code, true_index - index - 1, false_index - index - 1, k))

    stmt(_BPF_LD_W_ABS, 4)  # seccomp_data.arch
    jump(_BPF_JEQ, arch, nr_ld, deny_all_ret)
    stmt(_BPF_LD_W_ABS, 0)  # seccomp_data.nr
    jump(_BPF_JEQ, _NR_UNSHARE, deny_ret, clone_jmp)
    jump(_BPF_JEQ, clone_nr, args_ld, clone3_jmp)
    stmt(_BPF_LD_W_ABS, 16)  # seccomp_data.args[0].lo32
    jump(_BPF_JSET, _CLONE_NEWUSER, deny_ret, allow_ret)
    jump(_BPF_JEQ, _NR_CLONE3, clone3_ret, allow_ret)
    stmt(_BPF_RET_K, enosys)  # clone3: force fallback to clone
    stmt(_BPF_RET_K, deny)
    stmt(_BPF_RET_K, allow)
    stmt(_BPF_RET_K, enosys)  # unexpected arch fails closed
    assert len(instructions) == 12
    return b"".join(struct.pack("=HBBI", *ins) for ins in instructions)


def write_userns_seccomp(fd: int) -> None:
    """Write the old-bwrap nested-userns seccomp program to an inherited FD."""
    if fd < 0:
        raise IsolationUnavailable("invalid seccomp descriptor")
    program = _userns_seccomp_program()
    os.lseek(fd, 0, os.SEEK_SET)
    written = os.write(fd, program)
    if written != len(program):
        raise IsolationUnavailable("short write while preparing seccomp filter")
    os.ftruncate(fd, len(program))
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)


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
    if not isinstance(workspace, Workspace):
        # Guard against a bare Path (whose ``.root`` is ``/``) accidentally
        # binding the whole host filesystem into the sandbox.
        raise TypeError("sandbox workspace must be a Workspace, not a bare path")
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


def build_shell_argv(
    workspace: Workspace,
    command: str,
    *,
    empty_file_fd: Callable[[], int],
    private_paths: Sequence[Path] | None = None,
    seccomp_fd: Callable[[], int] | None = None,
) -> list[str]:
    """Bubblewrap argv for a project shell command.

    Only this project and a read-only host toolchain are mounted. Git metadata
    is withheld; credentials use the same path policy as file tools. Capabilities
    are dropped so a root caller cannot undo those mounts. Network is shared for
    dependency installation; this is a filesystem boundary, not network policy.
    Older bubblewrap hosts receive a host-owned seccomp FD instead of the newer
    ``--disable-userns`` options.
    """
    if not isinstance(workspace, Workspace):
        raise TypeError("sandbox workspace must be a Workspace, not a bare path")
    bwrap = _bwrap_path()
    if not bwrap:
        raise IsolationUnavailable("bubblewrap is unavailable")
    _validate_shell_root(workspace.root)
    root = str(workspace.root)
    argv: list[str] = [bwrap]
    for ro_path in _RO_BINDS:
        if os.path.exists(ro_path):
            argv += ["--ro-bind", ro_path, ro_path]
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
    ]
    # sys.base_prefix describes the already-running host interpreter, rather
    # than a project-controlled .venv symlink that could request another mount.
    python_root = Path(sys.base_prefix).resolve(strict=True)
    if python_root.is_relative_to(workspace.root) or python_root == Path("/"):
        raise IsolationUnavailable(
            "host Python installation is not an isolated toolchain"
        )
    if not any(python_root.is_relative_to(Path(path)) for path in _RO_BINDS):
        argv += ["--ro-bind", str(python_root), str(python_root)]
    tool_bin = "/run/cairn/bin"
    argv += ["--dir", tool_bin]
    original_interpreter = Path(getattr(sys, "_base_executable", sys.executable))
    interpreter = original_interpreter.resolve(strict=True)
    if original_interpreter != interpreter and not any(
        original_interpreter.is_relative_to(path)
        for path in (python_root, *(Path(path) for path in _RO_BINDS))
    ):
        # uv-created venvs may name a host-owned version alias. Expose only the
        # trusted executable alias, not its parent home or sibling installs.
        argv += ["--symlink", str(interpreter), str(original_interpreter)]
    argv += ["--symlink", str(interpreter), tool_bin + "/python"]
    uv = _host_uv(workspace)
    if uv is not None:
        argv += ["--ro-bind", str(uv), tool_bin + "/uv"]
    # Supply public resolver/CA data without exposing the rest of /etc.
    for system_file in (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        if Path(system_file).is_file():
            argv += ["--ro-bind", system_file, system_file]
    # A credential-shaped file may also be present in historical Git objects.
    # Withhold raw metadata instead of exposing it behind a file-only mask.
    if private_paths is None:
        private_paths = shell_private_paths(workspace.root)
    for private in private_paths:
        if private.is_symlink():
            raise IsolationUnavailable("private workspace path is a symlink")
        if private.is_dir():
            argv += ["--tmpfs", str(private), "--remount-ro", str(private)]
        else:
            argv += ["--ro-bind-data", str(empty_file_fd()), str(private)]
    userns_lock = _bwrap_supports_userns_lock(bwrap)
    isolation: list[str] = []
    if not userns_lock:
        if seccomp_fd is None:
            raise IsolationUnavailable(
                "old bubblewrap requires a host-owned seccomp descriptor"
            )
        descriptor = seccomp_fd()
        write_userns_seccomp(descriptor)
        isolation += ["--seccomp", str(descriptor)]
    isolation += [
        "--unshare-all",
        "--share-net",
        "--unshare-user",
        "--cap-drop",
        "ALL",
    ]
    if userns_lock:
        isolation += ["--disable-userns", "--assert-userns-disabled"]
    isolation += [
        "--die-with-parent",
        "--clearenv",
        "--setenv",
        "PATH",
        f"{root}/.venv/bin:{tool_bin}:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "UV_PYTHON",
        str(interpreter),
        "--setenv",
        "UV_PYTHON_DOWNLOADS",
        "never",
        "--setenv",
        "UV_CACHE_DIR",
        f"{root}/.cairn/cache/uv",
        "--setenv",
        "GIT_CONFIG_NOSYSTEM",
        "1",
        "--setenv",
        "GIT_CONFIG_GLOBAL",
        "/dev/null",
        "--setenv",
        "GIT_TERMINAL_PROMPT",
        "0",
        "--new-session",
        "--",
        "/bin/bash",
        "-o",
        "pipefail",
        "-c",
        command,
    ]
    argv += isolation
    return argv


def _host_uv(workspace: Workspace) -> Path | None:
    """Select a host binary without following a project-controlled PATH entry."""
    search = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if entry and not Path(entry).resolve().is_relative_to(workspace.root)
    )
    binary = shutil.which("uv", path=search)
    if binary is None:
        return None
    path = Path(binary).resolve(strict=True)
    if path.is_relative_to(workspace.root):
        raise IsolationUnavailable("uv toolchain points into the project")
    return path


def _validate_shell_root(root: Path) -> None:
    protected = (*_RO_BINDS, "/etc", "/sbin", "/boot", "/dev", "/proc", "/sys", "/run")
    if (
        root in (Path("/"), Path.home(), Path("/home"), Path("/tmp"))
        or is_credential_path(root)
        or any(part.casefold() == ".git" for part in root.parts)
        or any(root.is_relative_to(Path(path).resolve()) for path in protected)
    ):
        raise IsolationUnavailable("shell workspace overlaps a protected root")


def shell_private_paths(root: Path) -> list[Path]:
    """Bounded discovery; never follow links into another host directory."""
    _validate_shell_root(root)
    result: list[Path] = []
    examined = 0

    def fail(error: OSError) -> None:
        raise IsolationUnavailable(
            "workspace credential inventory could not complete"
        ) from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        examined += len(dirs) + len(files)
        if examined > 200_000:
            raise IsolationUnavailable(
                "workspace credential inventory exceeds its limit"
            )
        for name in list(dirs):
            path = Path(directory) / name
            if name.casefold() == ".git" or is_credential_path(path):
                result.append(path)
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            if name.casefold() == ".git" or is_credential_path(path):
                result.append(path)
    return result


class IsolatedRunner:
    """Runs host-owned scripts inside a fresh bubblewrap sandbox."""

    #: How long cleanup may take after a kill, independent of the caller's cancel.
    _CLEANUP_SECONDS = 5.0

    def __init__(self, limits: IsolationLimits | None = None) -> None:
        self.limits = limits or IsolationLimits()

    @staticmethod
    def available() -> bool:
        return available()

    async def run_script(self, workspace: Workspace, script: str) -> ToolResult:
        """Execute ``script`` isolated with bounded I/O and reliable cleanup.

        * stdout/stderr are streamed and capped (never fully buffered), so a child
          that floods output cannot exhaust parent memory or deadlock the pipe.
        * a timeout or an external cancellation kills the whole process group and
          reaps the reader tasks before the caller's error propagates; repeated
          cancellation cannot interrupt that cleanup, and no write survives it.
        """
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

        cap = self.limits.max_output_bytes
        out = _Capped()
        err = _Capped()
        readers = [
            asyncio.create_task(_read_capped(process.stdout, cap, out)),
            asyncio.create_task(_read_capped(process.stderr, cap, err)),
        ]
        waiter = asyncio.ensure_future(process.wait())
        try:
            await asyncio.wait_for(
                asyncio.gather(waiter, *readers), timeout=self.limits.timeout_seconds
            )
        except TimeoutError:
            await _terminate(process, waiter, readers)
            return ToolResult(
                stdout=out.decode(),
                stderr=f"behavior check exceeded {self.limits.timeout_seconds}s "
                "and was killed inside the sandbox",
                exit_code=-1,
                stdout_truncated=out.truncated,
                stderr_truncated=True,
            )
        except asyncio.CancelledError:
            # Kill the whole group and finish reaping before propagating, so a
            # cancelled run never keeps writing the workspace or leaks readers.
            await _terminate(process, waiter, readers)
            raise
        return ToolResult(
            stdout=out.decode(),
            stderr=err.decode(),
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
        )


class _Capped:
    """An output buffer that keeps at most ``cap`` bytes but never stops draining."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes, cap: int) -> None:
        if len(self._buf) < cap:
            take = cap - len(self._buf)
            self._buf += chunk[:take]
            if len(chunk) > take:
                self.truncated = True
        else:
            self.truncated = True

    def decode(self) -> str:
        return self._buf.decode("utf-8", "replace")


async def _read_capped(
    stream: asyncio.StreamReader | None, cap: int, sink: _Capped
) -> None:
    """Drain ``stream`` to EOF, keeping only the first ``cap`` bytes."""
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        sink.feed(chunk, cap)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), 9)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _await_quiet(future: asyncio.Future[Any], deadline: float) -> None:
    """Await ``future`` tolerating repeated cancellation until done or timed out."""
    loop = asyncio.get_running_loop()
    while not future.done() and loop.time() < deadline:
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            # A repeated cancel must not abandon cleanup; keep waiting.
            continue
        except Exception:
            return


async def _terminate(
    process: asyncio.subprocess.Process,
    waiter: asyncio.Future[Any],
    readers: list[asyncio.Task[Any]],
) -> None:
    """Kill the process group, then boundedly reap the waiter and readers.

    Runs to completion even when the surrounding task is cancelled again.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IsolatedRunner._CLEANUP_SECONDS
    _kill_group(process)
    await _await_quiet(waiter, deadline)
    if process.returncode is None:
        _kill_group(process)
        await _await_quiet(waiter, deadline)
    for reader in readers:
        if not reader.done():
            reader.cancel()
    for reader in readers:
        await _await_quiet(reader, deadline)


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
