"""Operation-owned local worktrees; Workspace itself has no lifecycle duties."""

import asyncio
import os
import shutil
import signal
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager, suppress
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from cairn.workspace.workspace import Workspace

GIT_TIMEOUT = 30.0
TERMINATE_TIMEOUT = 2.0
OUTPUT_LIMIT = 64 * 1024


class WorktreeError(RuntimeError):
    """Creation or cleanup could not safely complete."""


class _GitError(WorktreeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorktreeState(Enum):
    ACTIVE = "active"
    RETAINED = "retained"
    RELEASING = "releasing"
    RELEASED = "released"


async def _finish[T](operation: Coroutine[Any, Any, T]) -> T:
    """Observe cleanup to completion even under repeated external cancellation."""
    task = asyncio.create_task(operation)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:
            break
    if cancelled is not None:
        try:
            task.result()
        except BaseException as exc:
            cancelled.add_note(f"Cleanup failed: {exc!r}")
        raise cancelled
    return task.result()


async def _preserve(primary: BaseException, cleanup: Coroutine[Any, Any, None]) -> None:
    try:
        await _finish(cleanup)
    except BaseException as exc:
        primary.add_note(f"Worktree cleanup failed: {exc!r}")
        for note in getattr(exc, "__notes__", ()):
            primary.add_note(note)


async def _read(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    captured = bytearray()
    truncated = False
    while chunk := await stream.read(16 * 1024):
        remaining = OUTPUT_LIMIT - len(captured)
        captured.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return bytes(captured), truncated


async def _git(root: Path, *args: str) -> bytes:
    # Inherited Git routing variables must not redirect an operation to another
    # repository/index. Disable hooks for lifecycle commands, not agent tools.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    readers: list[asyncio.Task[tuple[bytes, bool]]] = []

    async def stop() -> None:
        try:
            process = await spawn
            if not readers:
                assert process.stdout is not None and process.stderr is not None
                readers.extend(
                    [
                        asyncio.create_task(_read(process.stdout)),
                        asyncio.create_task(_read(process.stderr)),
                    ]
                )
            # Kill the owned group too: filters may still hold pipe descriptors
            # after the Git parent has exited.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT)
            except TimeoutError:
                pass
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    try:
        async with asyncio.timeout(GIT_TIMEOUT):
            process = await asyncio.shield(spawn)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.create_task(_read(process.stdout)),
                asyncio.create_task(_read(process.stderr)),
            ]
            code = await process.wait()
            stdout, stderr = await asyncio.gather(*readers)
    except BaseException as exc:
        await _preserve(exc, stop())
        raise
    if stdout[1] or stderr[1]:
        raise WorktreeError("Git output exceeded capture limit")
    if code:
        raise _GitError(
            code,
            f"git {args[0]} failed ({code}): "
            f"{stderr[0].decode('utf-8', errors='replace').strip()}",
        )
    return stdout[0]


async def _is_symbolic(root: Path, ref: str) -> bool:
    try:
        await _git(root, "symbolic-ref", "--quiet", ref)
    except _GitError as exc:
        if exc.code != 1:
            raise
        return False
    return True


def _identity(path: Path) -> tuple[int, int]:
    stat = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise WorktreeError(f"Owned directory was replaced: {path}")
    return stat.st_dev, stat.st_ino


async def _registrations(root: Path) -> dict[Path, dict[bytes, bytes]]:
    output = await _git(root, "worktree", "list", "--porcelain", "-z")
    result: dict[Path, dict[bytes, bytes]] = {}
    for record in output.split(b"\0\0"):
        fields = dict(
            field.partition(b" ")[::2] for field in record.split(b"\0") if field
        )
        if b"worktree" in fields:
            result[Path(os.fsdecode(fields[b"worktree"]))] = fields
    return result


class WorktreeHandle:
    """Created by WorktreeProvider. Release is retryable and idempotent on success.

    ``remaining_branch`` reports a branch deliberately preserved during release.
    ``state`` stays active/retained if cleanup fails; inspect exception notes when
    a managed body or creation already has a primary exception.
    """

    def __init__(
        self,
        root: Path,
        path: Path,
        branch: str | None,
        revision: str,
        token: str,
        admin_path: Path,
    ) -> None:
        self.path = path
        self.branch = branch
        self.base_revision = revision
        self.state = WorktreeState.ACTIVE
        self.remaining_branch: str | None = None
        self._root = root
        self._token = token
        self._identity = _identity(path)
        self._gitfile: bytes | None = None
        self._admin: tuple[Path, tuple[int, int]] | None = None
        self._admin_path = admin_path
        self._removed = False

    @property
    def workspace(self) -> Workspace:
        return Workspace(self.path)

    def retain(self) -> None:
        if self.state not in (WorktreeState.ACTIVE, WorktreeState.RETAINED):
            raise WorktreeError(f"Cannot retain a {self.state.value} worktree")
        self.state = WorktreeState.RETAINED

    async def _remove_branch(self) -> None:
        if self.branch is None:
            return
        ref = f"refs/heads/{self.branch}"
        if await _is_symbolic(self._root, ref):
            self.remaining_branch = self.branch
            return
        refs = await _git(self._root, "for-each-ref", "--format=%(refname)", ref)
        if ref.encode() not in refs.splitlines():
            return
        log = await _git(self._root, "reflog", "show", "--format=%H %gs", ref)
        expected = f"{self.base_revision} {self._token}\n".encode()
        registrations = await _registrations(self._root)
        if log != expected or any(
            entry.get(b"branch") == ref.encode() for entry in registrations.values()
        ):
            self.remaining_branch = self.branch
            return
        # Compare-and-delete refuses a branch that moved since inspection.
        await _git(
            self._root, "update-ref", "-d", "--no-deref", ref, self.base_revision
        )

    async def _cleanup(self, discard_changes: bool) -> None:
        if not self._removed:
            exists = os.path.lexists(self.path)
            if exists and _identity(self.path) != self._identity:
                raise WorktreeError(f"Owned directory was replaced: {self.path}")
            entries = await _registrations(self._root)
            if (
                self._gitfile is None
                and os.path.lexists(self._admin_path)
                and not os.path.lexists(self.path / ".git")
            ):
                # Interrupted add: Git may list the admin record but cannot
                # remove it without the working tree's .git backlink.
                self._remove_partial_registration()
                if exists:
                    shutil.rmtree(self.path)
            elif self.path in entries:
                if self._admin is not None:
                    admin, identity = self._admin
                    if _identity(admin) != identity or (
                        admin / "gitdir"
                    ).read_bytes().rstrip(b"\n") != os.fsencode(self.path / ".git"):
                        raise WorktreeError(
                            f"Worktree registration changed: {self.path}"
                        )
                if (
                    exists
                    and self._gitfile is not None
                    and (self.path / ".git").read_bytes() != self._gitfile
                ):
                    raise WorktreeError(f"Worktree registration changed: {self.path}")
                if not discard_changes:
                    status = await _git(
                        self.path,
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                        "--ignored",
                    )
                    if status:
                        raise WorktreeError(
                            f"Dirty worktree preserved: {self.path}; "
                            "use release(discard_changes=True) to discard changes"
                        )
                args = ["worktree", "remove"]
                if discard_changes:
                    args.append("--force")
                await _git(self._root, *args, "--", str(self.path))
            elif exists:
                # During failed creation this is our reserved directory. After
                # creation, loss of registration is ambiguous: preserve it.
                if self._gitfile is not None:
                    raise WorktreeError(f"Worktree registration missing: {self.path}")
                if os.path.lexists(self._admin_path):
                    self._remove_partial_registration()
                shutil.rmtree(self.path)
            if (
                os.path.lexists(self.path)
                or os.path.lexists(self._admin_path)
                or self.path in await _registrations(self._root)
            ):
                raise WorktreeError(f"Worktree removal incomplete: {self.path}")
            self._removed = True
        await self._remove_branch()

    def _remove_partial_registration(self) -> None:
        # This operation-specific path was absent before creation. Require the
        # exact backlink too; never prune unrelated or unidentifiable records.
        _identity(self._admin_path)
        if (self._admin_path / "gitdir").read_bytes().rstrip(b"\n") != os.fsencode(
            self.path / ".git"
        ):
            raise WorktreeError(
                f"Unidentifiable partial registration: {self._admin_path}"
            )
        shutil.rmtree(self._admin_path)

    async def release(self, discard_changes: bool = False) -> None:
        if self.state == WorktreeState.RELEASED:
            return
        if self.state == WorktreeState.RELEASING:
            raise WorktreeError("Worktree release already in progress")
        previous = self.state
        self.state = WorktreeState.RELEASING

        async def release_owned() -> None:
            try:
                await self._cleanup(discard_changes)
            except BaseException:
                self.state = previous
                raise
            self.state = WorktreeState.RELEASED

        await _finish(release_owned())


class WorktreeProvider:
    """Create isolated workspaces from a local Git repository.

    Parent directories are shared caller infrastructure, never removed. Managed
    scopes discard ephemeral edits on exit unless retained. Explicit release
    defaults to preserving dirty files (including ignored files).
    """

    def __init__(self, source: Workspace, parent: Path) -> None:
        self.source = source
        self.parent = parent.resolve()

    async def create(self, base_ref: str, branch: str | None) -> WorktreeHandle:
        bare = (
            await _git(self.source.root, "rev-parse", "--is-bare-repository")
        ).strip() == b"true"
        root = Path(
            os.fsdecode(
                (
                    await _git(
                        self.source.root,
                        "rev-parse",
                        "--absolute-git-dir" if bare else "--show-toplevel",
                    )
                ).rstrip(b"\n")
            )
        ).resolve()
        if self.parent.is_relative_to(root):
            raise WorktreeError(
                "Worktree parent must be outside the source working tree"
            )
        if not base_ref or "\0" in base_ref:
            raise WorktreeError("Invalid base revision")
        revision = (
            (
                await _git(
                    root,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{base_ref}^{{commit}}",
                )
            )
            .decode()
            .strip()
        )
        if branch is not None:
            if (
                not branch
                or branch.startswith("-")
                or "\0" in branch
                or branch == "HEAD"
            ):
                raise WorktreeError("Invalid branch name")
            await _git(root, "check-ref-format", f"refs/heads/{branch}")
            if await _is_symbolic(root, f"refs/heads/{branch}"):
                raise WorktreeError(
                    f"Branch already exists as a symbolic ref: {branch}"
                )
        token = f"cairn-worktree-{uuid4().hex}"
        common = Path(
            os.fsdecode(
                (
                    await _git(
                        root, "rev-parse", "--path-format=absolute", "--git-common-dir"
                    )
                ).rstrip(b"\n")
            )
        )
        admin_path = common / "worktrees" / token
        if os.path.lexists(admin_path):
            raise WorktreeError(
                f"Worktree registration path already exists: {admin_path}"
            )
        self.parent.mkdir(parents=True, exist_ok=True)
        path = self.parent / token
        path.mkdir()  # Atomic reservation: never reuse a directory, even empty.
        handle = WorktreeHandle(root, path, branch, revision, token, admin_path)
        try:
            if branch is not None:
                # An absent-old-value CAS creates exactly one new ref. The
                # unique reflog entry also identifies interrupted creations.
                await _git(
                    root,
                    "update-ref",
                    "--create-reflog",
                    "--no-deref",
                    "-m",
                    token,
                    f"refs/heads/{branch}",
                    revision,
                    "0" * len(revision),
                )
                await _git(root, "worktree", "add", "--", str(path), branch)
            else:
                await _git(
                    root, "worktree", "add", "--detach", "--", str(path), revision
                )
            entry = (await _registrations(root)).get(path)
            expected_branch = (
                None if branch is None else f"refs/heads/{branch}".encode()
            )
            if (
                entry is None
                or entry.get(b"HEAD") != revision.encode()
                or entry.get(b"branch") != expected_branch
            ):
                raise WorktreeError(
                    "Created worktree registration does not match request"
                )
            handle._gitfile = (path / ".git").read_bytes()
            admin = Path(
                os.fsdecode(handle._gitfile.removeprefix(b"gitdir: ").rstrip(b"\n"))
            )
            if admin != admin_path or (admin / "gitdir").read_bytes().rstrip(
                b"\n"
            ) != os.fsencode(path / ".git"):
                raise WorktreeError(
                    "Created worktree is not registered in the source repository"
                )
            handle._admin = admin, _identity(admin)
            if (await _git(path, "rev-parse", "HEAD")).decode().strip() != revision:
                raise WorktreeError(
                    "Created worktree HEAD does not match requested revision"
                )
            _ = handle.workspace  # Validate the ordinary Workspace before returning.
            return handle
        except BaseException as exc:
            await _preserve(exc, handle.release(discard_changes=True))
            if handle.remaining_branch is not None:
                exc.add_note(f"Branch preserved: {handle.remaining_branch}")
            raise

    @asynccontextmanager
    async def managed(
        self, base_ref: str, branch: str | None
    ) -> AsyncIterator[WorktreeHandle]:
        handle = await self.create(base_ref, branch)
        try:
            yield handle
        except BaseException as exc:
            if handle.state != WorktreeState.RETAINED:
                await _preserve(exc, handle.release(discard_changes=True))
                if handle.remaining_branch is not None:
                    exc.add_note(f"Branch preserved: {handle.remaining_branch}")
            raise
        else:
            if handle.state != WorktreeState.RETAINED:
                await handle.release(discard_changes=True)
