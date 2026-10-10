"""Bounded exact review input using the existing owned-worktree Git policy."""

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cairn.git.worktree import WorktreeError
from cairn.review.models import ReviewStatus
from cairn.workflow.git import SnapshotDriftError, WorkflowGit
from cairn.workflow.models import GitSnapshot, VerificationResult

MAX_DIFF_BYTES = 32 * 1024
MAX_WORKSPACE_ENTRIES = 10_000


class ReviewInputError(ValueError):
    def __init__(self, status: ReviewStatus) -> None:
        self.status = status
        super().__init__(status.value)


@dataclass(frozen=True, slots=True)
class ReviewInput:
    diff: str
    workspace_state: bytes


class _ReadOnlyGit(WorkflowGit):
    async def run(self, *args: str) -> bytes:
        # git status may otherwise replace the index merely to refresh stat
        # bookkeeping. Review must preserve even those optional index writes.
        return await super().run("--no-optional-locks", *args)

    async def _assert_index(self, snapshot: GitSnapshot) -> None:
        await self._index_entries()
        changed = await self.run(
            "diff",
            "--cached",
            "--raw",
            "-z",
            "--no-relative",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            snapshot.tree_revision,
            "--",
        )
        if changed:
            raise SnapshotDriftError("Staged tree changed before or during review")

    async def assert_unchanged(self, snapshot: GitSnapshot) -> None:
        # write-tree is an explicit index writer, even with optional locks off.
        # Compare the live index directly to the immutable tree instead, then
        # reuse the existing materialized/filter/untracked ownership checks.
        head, branch = await self._head_branch()
        if (head, branch) != (snapshot.head_revision, snapshot.branch):
            raise SnapshotDriftError("HEAD or branch changed before review")
        await self._assert_index(snapshot)
        await self._git.check_filters(self.handle.path)
        await self._git.check_attributes(self.handle.path, snapshot.tree_revision)
        await self._assert_materialized()
        await self._assert_no_unstaged()
        if await self._head_branch() != (head, branch):
            raise SnapshotDriftError("HEAD or branch changed during review")
        await self._assert_index(snapshot)


def validate_verification(
    snapshot: GitSnapshot, verification: VerificationResult | None
) -> None:
    try:
        if (
            type(snapshot) is not GitSnapshot
            or type(verification) is not VerificationResult
        ):
            raise ValueError
        snapshot.__post_init__()
        verification.validate()
        if (
            not verification.passed
            or verification.tree_revision != snapshot.tree_revision
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ReviewInputError(ReviewStatus.INVALID_VERIFICATION) from None


def _identity(value: os.stat_result) -> tuple[int, ...]:
    # Reading may update atime. Every other observable identity/mode/time fact
    # matters for proving this read-only phase left its inputs untouched.
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


async def _workspace_state(git: WorkflowGit) -> bytes:
    digest = hashlib.sha256()
    count = 0
    pending = [git.handle.path]
    # The private index/admin is outside the Workspace; include it separately.
    pending.append(git.handle._admin_path)
    common = git.handle._admin_path.parent.parent
    pending.extend(
        path
        for path in (common / "config", common / "config.worktree")
        if path.exists()
    )
    try:
        while pending:
            path = pending.pop()
            count += 1
            if count > MAX_WORKSPACE_ENTRIES:
                raise ReviewInputError(ReviewStatus.INCOMPLETE)
            before = path.lstat()
            digest.update(
                os.fsencode(path) + b"\0" + repr(_identity(before)).encode() + b"\0"
            )
            if stat.S_ISDIR(before.st_mode):
                with os.scandir(path) as entries:
                    children: list[Path] = []
                    for entry in entries:
                        if (
                            count + len(pending) + len(children)
                            >= MAX_WORKSPACE_ENTRIES
                        ):
                            raise ReviewInputError(ReviewStatus.INCOMPLETE)
                        children.append(Path(entry.path))
                pending.extend(sorted(children, key=os.fsencode, reverse=True))
            elif stat.S_ISLNK(before.st_mode):
                digest.update(os.fsencode(os.readlink(path)) + b"\0")
            elif stat.S_ISREG(before.st_mode):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if _identity(os.fstat(stream.fileno())) != _identity(before):
                        raise SnapshotDriftError(
                            "Review input changed during inspection"
                        )
                    while chunk := stream.read(16 * 1024):
                        digest.update(chunk)
                        await asyncio.sleep(0)
            else:
                raise ReviewInputError(ReviewStatus.INCOMPLETE)
            if _identity(path.lstat()) != _identity(before):
                raise SnapshotDriftError("Review input changed during inspection")
            await asyncio.sleep(0)
    except OSError:
        raise SnapshotDriftError("Review filesystem input is unavailable") from None
    return digest.digest()


async def prepare_review(
    git: WorkflowGit, snapshot: GitSnapshot, verification: VerificationResult | None
) -> ReviewInput:
    git = _ReadOnlyGit(git.handle)
    validate_verification(snapshot, verification)
    if (
        snapshot.branch != git.handle.branch
        or snapshot.head_revision != git.handle.base_revision
    ):
        raise ReviewInputError(ReviewStatus.SNAPSHOT_DRIFT)
    try:
        await git.assert_unchanged(snapshot)
        options = (
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-relative",
        )
        # Match stage()'s rename-aware path list. The full patch below still
        # expands renames into deletion/addition so no code is omitted.
        paths = await git.run(
            "diff",
            *options,
            "--name-only",
            "-z",
            snapshot.head_revision,
            snapshot.tree_revision,
            "--",
        )
        actual_paths = tuple(
            sorted(os.fsdecode(path) for path in paths.split(b"\0") if path)
        )
        if actual_paths != snapshot.changed_files:
            raise ReviewInputError(ReviewStatus.INVALID_VERIFICATION)
        stats = await git.run(
            "diff",
            *options,
            "--no-renames",
            "--numstat",
            "-z",
            snapshot.head_revision,
            snapshot.tree_revision,
            "--",
        )
        if any(record.startswith(b"-\t-\t") for record in stats.split(b"\0")):
            raise ReviewInputError(ReviewStatus.INCOMPLETE)
        patch = await git.run(
            "diff",
            *options,
            "--no-renames",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            snapshot.head_revision,
            snapshot.tree_revision,
            "--",
        )
        if not patch or len(patch) > MAX_DIFF_BYTES:
            raise ReviewInputError(ReviewStatus.INCOMPLETE)
        diff = patch.decode("utf-8", errors="strict")
        await git.assert_unchanged(snapshot)
        return ReviewInput(diff, await _workspace_state(git))
    except SnapshotDriftError:
        raise ReviewInputError(ReviewStatus.SNAPSHOT_DRIFT) from None
    except (WorktreeError, UnicodeError):
        raise ReviewInputError(ReviewStatus.INCOMPLETE) from None


async def assert_review_unchanged(
    git: WorkflowGit, snapshot: GitSnapshot, workspace_state: bytes
) -> None:
    git = _ReadOnlyGit(git.handle)
    await git.assert_unchanged(snapshot)
    try:
        if await _workspace_state(git) != workspace_state:
            raise SnapshotDriftError(
                "Read-only review changed its workspace or Git inputs"
            )
    except ReviewInputError:
        raise SnapshotDriftError(
            "Review input could not be completely reinspected"
        ) from None


async def assert_snapshot_unchanged(git: WorkflowGit, snapshot: GitSnapshot) -> None:
    """Use the existing snapshot invariants without writing the live index."""
    await _ReadOnlyGit(git.handle).assert_unchanged(snapshot)
