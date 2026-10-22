"""Descriptor-relative filesystem primitives shared across infra observers.

Repository inventory and project memory both treat the workspace as a trust
boundary: they open the root one component at a time with ``O_NOFOLLOW`` and
re-check stat identity through open descriptors, so a replaced ancestor symlink
can never redirect a read outside the workspace. Keeping that walk here means
the security semantics have one implementation used by both consumers.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# POSIX-only flags. Reading a missing one as 0 keeps this module importable on a
# host that lacks it (Windows); the callers that need the protection refuse there.
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class StatIdentity:
    """The stat fields that identify one filesystem object across a read."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, result: os.stat_result) -> "StatIdentity":
        return cls(
            result.st_dev,
            result.st_ino,
            result.st_mode,
            result.st_size,
            result.st_mtime_ns,
            result.st_ctime_ns,
        )


def open_root(root: Path) -> int:
    """Open ``root`` descriptor-relatively, following no symlinked component.

    The whole path after the anchor is walked, not just the final root, because
    ``O_NOFOLLOW`` on the last component alone still follows a replaced ancestor
    symlink. A symlinked, replaced or otherwise unopenable component raises
    ``OSError`` instead of redirecting the caller; callers fail closed. The
    caller owns and must close the returned descriptor.
    """
    fd = os.open(root.anchor, _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise
