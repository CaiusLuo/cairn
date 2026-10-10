"""Git evidence for the exact index and its materialized verifier input."""

import asyncio
import hashlib
import os
import stat

from cairn.git.worktree import (
    WorktreeError,
    WorktreeHandle,
    WorktreeState,
    _identity,
)
from cairn.workflow.models import GitSnapshot


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class NoChangesError(WorktreeError):
    """The owned worktree contains no proposed tree change."""


class SnapshotDriftError(WorktreeError):
    """The owned worktree no longer matches the verified proposal."""


class WorkflowGit:
    """Use the provider's environment/process policy and validate its ownership."""

    def __init__(self, handle: WorktreeHandle) -> None:
        self.handle = handle
        self._git = handle._git
        self.assert_owned()
        assert handle._admin is not None
        self._commondir = (handle._admin[0] / "commondir").read_bytes()

    @property
    def env(self) -> dict[str, str]:
        return dict(self._git.env)

    def assert_owned(self) -> None:
        try:
            self._assert_owned()
        except SnapshotDriftError:
            raise
        except (OSError, WorktreeError):
            raise SnapshotDriftError(
                "Owned worktree registration is unavailable"
            ) from None

    def _assert_owned(self) -> None:
        handle = self.handle
        if handle.state not in (WorktreeState.ACTIVE, WorktreeState.RETAINED):
            raise SnapshotDriftError("Worktree is no longer retained or active")
        if _identity(handle.path) != handle._identity:
            raise SnapshotDriftError("Owned worktree directory changed")
        gitfile = handle.path / ".git"
        if (
            handle._gitfile is None
            or gitfile.is_symlink()
            or gitfile.read_bytes() != handle._gitfile
            or handle._admin is None
        ):
            raise SnapshotDriftError("Owned worktree Git registration changed")
        admin, identity = handle._admin
        if (
            _identity(admin) != identity
            or (admin / "gitdir").is_symlink()
            or (admin / "gitdir").read_bytes().rstrip(b"\n") != os.fsencode(gitfile)
            or (admin / "commondir").is_symlink()
        ):
            raise SnapshotDriftError("Owned worktree admin registration changed")
        common = os.fsdecode((admin / "commondir").read_bytes().rstrip(b"\n"))
        if (admin / common).resolve() != handle._admin_path.parent.parent.resolve():
            raise SnapshotDriftError("Owned worktree common directory changed")
        if (
            hasattr(self, "_commondir")
            and (admin / "commondir").read_bytes() != self._commondir
        ):
            raise SnapshotDriftError("Owned worktree common directory changed")

    async def run(self, *args: str) -> bytes:
        """Run trusted harness arguments in the provider's scrubbed Git context."""
        await self.assert_context()
        self.assert_owned()
        return await self._git(self.handle.path, *args)

    async def assert_context(self) -> None:
        """Reject config that redirects Git away from the owned paths."""
        self.assert_owned()
        handle = self.handle
        if (handle._admin_path / "index").is_symlink():
            raise SnapshotDriftError("Owned worktree index was replaced")
        # Call the provider executor directly: invoking run() would recurse.
        try:
            coordinates = await self._git(
                handle.path,
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
                "--git-path",
                "index",
            )
        except WorktreeError:
            raise SnapshotDriftError("Owned Git context is unavailable") from None
        expected = b"".join(
            os.fsencode(path) + b"\n"
            for path in (
                handle.path,
                handle._admin_path,
                handle._admin_path.parent.parent,
                handle._admin_path / "index",
            )
        )
        if coordinates != expected:
            raise SnapshotDriftError("Git configuration redirected the owned worktree")

    async def _head_branch(self) -> tuple[str, str]:
        head = (
            (await self.run("rev-parse", "--verify", "HEAD^{commit}"))
            .decode("ascii")
            .strip()
        )
        branch = (await self.run("branch", "--show-current")).decode().strip()
        return head, branch

    async def _current_attributes(self) -> None:
        # add reads working-tree attributes. HEAD-only checks miss new rules.
        await self._git.check_filters(self.handle.path)
        paths = (
            await self.run(
                "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )
        ).split(b"\0")
        batch: list[str] = []
        size = 0

        async def inspect() -> None:
            if not batch:
                return
            output = await self.run("check-attr", "-z", "filter", "--", *batch)
            fields = output.split(b"\0")
            if fields[-1:] != [b""] or len(fields[:-1]) != 3 * len(batch):
                raise WorktreeError("Malformed Git attribute inspection")
            if any(value not in {b"unset", b"unspecified"} for value in fields[2::3]):
                raise WorktreeError("Workflow rejects external filter attributes")

        for path in paths:
            if not path:
                continue
            if batch and (len(batch) == 64 or size + len(path) > 16 * 1024):
                await inspect()
                batch.clear()
                size = 0
            batch.append(os.fsdecode(path))
            size += len(path)
        await inspect()

    async def _index_entries(self) -> list[tuple[bytes, bytes, bytes]]:
        output = await self.run("ls-files", "--stage", "-v", "-z")
        entries: list[tuple[bytes, bytes, bytes]] = []
        for record in output.split(b"\0"):
            if not record:
                continue
            if record[:1].islower() or record[:1] == b"S":
                raise SnapshotDriftError("Unsupported hidden index entries")
            metadata, separator, path = record[2:].partition(b"\t")
            parts = metadata.split(b" ")
            if (
                record[1:2] != b" "
                or not separator
                or not path
                or len(parts) != 3
                or parts[2] != b"0"
            ):
                raise SnapshotDriftError("Unsupported or unmerged index entry")
            mode, revision = parts[:2]
            if mode not in {b"100644", b"100755", b"120000"}:
                raise SnapshotDriftError("Unsupported index file mode")
            entries.append((mode, revision, path))
        return entries

    async def _assert_materialized(self) -> None:
        try:
            await self._inspect_materialized()
        except OSError:
            raise SnapshotDriftError("Staged filesystem input is unavailable") from None

    async def _inspect_materialized(self) -> None:
        # Compare bytes, not cached mtime/stat data or a bounded path summary.
        for mode, revision, raw_path in await self._index_entries():
            self.assert_owned()
            path = self.handle.path / os.fsdecode(raw_path)
            if path.parent.resolve() != path.parent:
                raise SnapshotDriftError("Index parent directory was replaced")
            before = path.lstat()
            digest = hashlib.new("sha1" if len(revision) == 40 else "sha256")
            if mode == b"120000":
                if not stat.S_ISLNK(before.st_mode):
                    raise SnapshotDriftError("Staged symlink changed")
                content = os.fsencode(os.readlink(path))
                digest.update(f"blob {len(content)}\0".encode())
                digest.update(content)
            else:
                if not stat.S_ISREG(before.st_mode) or bool(before.st_mode & 0o111) != (
                    mode == b"100755"
                ):
                    raise SnapshotDriftError("Staged file type or mode changed")
                digest.update(f"blob {before.st_size}\0".encode())
                with path.open("rb") as stream:
                    while chunk := stream.read(16 * 1024):
                        digest.update(chunk)
                        await asyncio.sleep(0)
            after = path.lstat()
            if (
                _file_identity(before) != _file_identity(after)
                or digest.hexdigest().encode() != revision
            ):
                raise SnapshotDriftError("Working bytes differ from the staged tree")

    async def _assert_no_unstaged(self) -> None:
        output = await self.run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"
        )
        records = iter(output.split(b"\0"))
        for record in records:
            if not record:
                continue
            if len(record) < 4 or record[2:3] != b" " or record[1:2] != b" ":
                raise SnapshotDriftError("Unstaged or untracked worktree drift")
            if record[:1] in {b"R", b"C"}:
                next(records, None)  # Rename/copy source is not a second status.

    async def stage(self) -> GitSnapshot:
        head, branch = await self._head_branch()
        if head != self.handle.base_revision or branch != self.handle.branch:
            raise SnapshotDriftError("Task moved the owned base or branch")
        await self._index_entries()
        await self._current_attributes()
        await self.run("add", "--all", "--", ".")
        tree = (await self.run("write-tree")).decode("ascii").strip()
        await self._git.check_attributes(self.handle.path, tree)
        output = await self.run(
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            head,
            "--",
        )
        changed = tuple(
            sorted(os.fsdecode(path) for path in output.split(b"\0") if path)
        )
        if not changed:
            raise NoChangesError("Task produced no staged changes")
        snapshot = GitSnapshot(head, tree, branch, changed)
        await self.assert_unchanged(snapshot)
        return snapshot

    async def assert_unchanged(self, snapshot: GitSnapshot) -> None:
        head, branch = await self._head_branch()
        tree = (await self.run("write-tree")).decode("ascii").strip()
        if (head, branch, tree) != (
            snapshot.head_revision,
            snapshot.branch,
            snapshot.tree_revision,
        ):
            raise SnapshotDriftError("HEAD, branch or staged tree changed")
        await self._git.check_filters(self.handle.path)
        await self._git.check_attributes(self.handle.path, snapshot.tree_revision)
        await self._assert_materialized()
        await self._assert_no_unstaged()
        # Observe identity again after the filesystem scan's cancellation points.
        if (
            await self._head_branch() != (head, branch)
            or (await self.run("write-tree")).decode("ascii").strip() != tree
        ):
            raise SnapshotDriftError("Proposal changed during drift inspection")
