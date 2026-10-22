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

from cairn.infra import platform


# POSIX-only flags, resolved inside ``open_root`` so import stays safe on a host
# that lacks one while the walk itself refuses.
def _directory_flags() -> int:
    return platform.open_flags("O_DIRECTORY", "O_NOFOLLOW")


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
    # Refuse before opening anything when the host cannot anchor the walk to a
    # descriptor or cannot refuse a replaced symlink. ``OSError`` keeps the
    # callers' existing failure handling.
    platform.require_dir_fd()
    platform.require_no_follow_stat()
    fd = os.open(root.anchor, _directory_flags())
    try:
        for component in root.parts[1:]:
            child_fd = os.open(component, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise
