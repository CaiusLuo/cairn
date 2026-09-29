import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.workspace.workspace import Workspace

DEFAULT_GIT_TIMEOUT = 2.0
DEFAULT_MAX_PATHS = 50
GIT_OUTPUT_CAPTURE_LIMIT = 64 * 1024
GIT_READ_CHUNK_SIZE = 16 * 1024


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    workspace_root: Path
    repository_root: Path | None
    branch: str | None
    dirty: bool | None
    changed_files: tuple[str, ...] = ()
    untracked_files: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def is_git_repository(self) -> bool:
        return self.repository_root is not None

    def to_prompt(self) -> str:
        if not self.is_git_repository:
            return (
                "Runtime workspace context:\n"
                f"- Workspace: {self.workspace_root}\n"
                "- Git repository: no"
            )

        changed = [f"  - {path}" for path in self.changed_files] or ["  - none"]
        untracked = [f"  - {path}" for path in self.untracked_files] or ["  - none"]
        lines = [
            "Runtime repository context:",
            f"- Workspace: {self.workspace_root}",
            f"- Repository root: {self.repository_root}",
            f"- Branch: {self.branch}",
            f"- Status: {'dirty' if self.dirty else 'clean'}",
            "- Changed files:",
            *changed,
            "- Untracked files:",
            *untracked,
        ]
        if self.truncated:
            lines.append("- File list truncated: yes")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    captured = bytearray()
    truncated = False

    while True:
        chunk = await stream.read(GIT_READ_CHUNK_SIZE)
        if not chunk:
            return bytes(captured), truncated

        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True


async def _read_status_paths(
    stream: asyncio.StreamReader,
    *,
    max_paths: int,
) -> tuple[list[str], bool]:
    lines: list[str] = []
    while len(lines) <= max_paths:
        line = await stream.readline()
        if not line:
            return lines, False
        lines.append(line.decode("utf-8", errors="strict").rstrip("\n"))
    return lines[:max_paths], True


async def _settle_tasks(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class RepoContextProvider:
    def __init__(
        self,
        workspace: Workspace,
        *,
        max_paths: int = DEFAULT_MAX_PATHS,
        timeout: float = DEFAULT_GIT_TIMEOUT,
    ) -> None:
        if max_paths < 1:
            raise ValueError("max_paths must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        self.workspace = workspace
        self.max_paths = max_paths
        self.timeout = timeout

    async def _start_git(self, *args: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.workspace.root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def _run_git(self, *args: str) -> _GitCommandResult:
        process = await self._start_git(*args)
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, limit=GIT_OUTPUT_CAPTURE_LIMIT)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, limit=GIT_OUTPUT_CAPTURE_LIMIT)
        )

        try:
            async with asyncio.timeout(self.timeout):
                returncode = await process.wait()
                stdout, stdout_truncated = await stdout_task
                stderr, stderr_truncated = await stderr_task
        except BaseException:
            with suppress(Exception):
                await self._terminate(process)
            await _settle_tasks(stdout_task, stderr_task)
            raise

        return _GitCommandResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    async def _read_status(self) -> tuple[list[str], bool]:
        process = await self._start_git(
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--",
            ".",
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_status_paths(process.stdout, max_paths=self.max_paths)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, limit=GIT_OUTPUT_CAPTURE_LIMIT)
        )
        drain_task: asyncio.Task[tuple[bytes, bool]] | None = None

        try:
            async with asyncio.timeout(self.timeout):
                lines, truncated = await stdout_task
                if truncated:
                    # Drain only pipe data left after termination so process.wait()
                    # cannot stall; no further status records are retained.
                    drain_task = asyncio.create_task(
                        _read_bounded(process.stdout, limit=0)
                    )
                    await self._terminate(process)
                returncode = await process.wait()
                stderr, _ = await stderr_task
                if drain_task is not None:
                    await drain_task
        except BaseException:
            with suppress(Exception):
                await self._terminate(process)
            tasks = [stdout_task, stderr_task]
            if drain_task is not None:
                tasks.append(drain_task)
            await _settle_tasks(*tasks)
            raise

        if returncode != 0 and not truncated:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git status failed: {detail or returncode}")
        return lines, truncated

    async def inspect(self) -> RepositoryContext:
        repository = await self._run_git("rev-parse", "--show-toplevel")
        if repository.returncode != 0:
            return RepositoryContext(
                workspace_root=self.workspace.root,
                repository_root=None,
                branch=None,
                dirty=None,
            )
        if repository.stdout_truncated:
            raise RuntimeError("git repository root output exceeded capture limit")

        repository_root = Path(repository.stdout.decode("utf-8").strip()).resolve()

        branch_result = await self._run_git("branch", "--show-current")
        branch = None
        if branch_result.returncode == 0 and not branch_result.stdout_truncated:
            branch = branch_result.stdout.decode("utf-8").strip() or None
        if branch is None:
            head = await self._run_git("rev-parse", "--short", "HEAD")
            if head.returncode == 0 and not head.stdout_truncated:
                revision = head.stdout.decode("utf-8").strip()
                if revision:
                    branch = f"detached@{revision}"

        status_lines, truncated = await self._read_status()
        changed_files: list[str] = []
        untracked_files: list[str] = []
        for line in status_lines:
            if len(line) < 4:
                continue
            path = line[3:]
            if line.startswith("??"):
                untracked_files.append(path)
            else:
                changed_files.append(path)

        return RepositoryContext(
            workspace_root=self.workspace.root,
            repository_root=repository_root,
            branch=branch,
            dirty=bool(status_lines),
            changed_files=tuple(changed_files),
            untracked_files=tuple(untracked_files),
            truncated=truncated,
        )
