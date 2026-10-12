"""Descriptor-relative, bounded observations; never follow dependency symlinks."""

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cairn.core.memory.models import Fingerprint, MemoryError, PathKind, safe_path


@dataclass(frozen=True, slots=True)
class ReadLimits:
    file_bytes: int = 64 * 1024
    total_bytes: int = 512 * 1024
    members: int = 256
    seconds: float = 2.0

    def __post_init__(self) -> None:
        if not (
            1 <= self.file_bytes <= 65536
            and 1 <= self.total_bytes <= 1048576
            and 1 <= self.members <= 256
            and 0 < self.seconds <= 5
        ):
            raise ValueError("Invalid memory read limits")


def kind(mode: int) -> PathKind:
    if stat.S_ISLNK(mode):
        return PathKind.SYMLINK
    if stat.S_ISDIR(mode):
        return PathKind.DIRECTORY
    if stat.S_ISREG(mode):
        return PathKind.FILE
    raise MemoryError("Unsupported memory evidence file type")


def git_mode(mode: int) -> str:
    file_kind = kind(mode)
    if file_kind is PathKind.DIRECTORY:
        return "040000"
    if file_kind is PathKind.SYMLINK:
        return "120000"
    return "100755" if mode & 0o111 else "100644"


def membership_digest(members: list[tuple[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(members), ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _root_fd(root: Path) -> int:
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


class FileObserver:
    def __init__(self, root: Path, limits: ReadLimits | None = None) -> None:
        self.root = root
        self.limits = limits or ReadLimits()

    def capture(self, paths: tuple[str, ...]) -> tuple[Fingerprint, ...]:
        if not 1 <= len(paths) <= 16:
            raise MemoryError("Memory dependency limit exceeded")
        deadline = time.monotonic() + self.limits.seconds
        remaining = [self.limits.total_bytes]

        def check() -> None:
            if time.monotonic() > deadline:
                raise MemoryError("Memory evidence read limit exceeded")

        fd: int | None = None
        try:
            fd = _root_fd(self.root)
            root_identity = os.fstat(fd)
            results = tuple(self._one(fd, path, remaining, check) for path in paths)
            # A second descriptor walk catches replaced intermediate directories and
            # membership/mode changes that do not change the root directory's stat.
            repeated = tuple(self._one(fd, path, remaining, check) for path in paths)
            if repeated != results:
                raise MemoryError("Memory evidence changed during inspection")
            other = _root_fd(self.root)
            try:
                current = os.fstat(other)
                if (root_identity.st_dev, root_identity.st_ino) != (
                    current.st_dev,
                    current.st_ino,
                ):
                    raise MemoryError("Memory evidence changed during inspection")
            finally:
                os.close(other)
            check()
            return results
        except (OSError, ValueError):
            raise MemoryError("Memory evidence unreadable") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _one(
        self, root_fd: int, path: str, remaining: list[int], check: Callable[[], None]
    ) -> Fingerprint:
        safe_path(path)
        check()
        parent = os.dup(root_fd)
        try:
            parts = path.split("/")
            for component in parts[:-1]:
                try:
                    info = os.stat(component, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return Fingerprint(path=path, kind=PathKind.MISSING, mode=0)
                if stat.S_ISLNK(info.st_mode):
                    return Fingerprint(
                        path=path,
                        kind=PathKind.SYMLINK,
                        mode=stat.S_IMODE(info.st_mode),
                    )
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
                os.close(parent)
                parent = next_fd
            name = parts[-1]
            try:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return Fingerprint(path=path, kind=PathKind.MISSING, mode=0)
            file_kind = kind(before.st_mode)
            if file_kind is PathKind.SYMLINK:
                return Fingerprint(
                    path=path, kind=file_kind, mode=stat.S_IMODE(before.st_mode)
                )
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if file_kind is PathKind.DIRECTORY:
                flags |= os.O_DIRECTORY
            fd = os.open(name, flags, dir_fd=parent)
            try:
                if _signature(before) != _signature(os.fstat(fd)):
                    raise MemoryError("Memory evidence changed during inspection")
                members: list[tuple[str, str]] = []
                if file_kind is PathKind.DIRECTORY:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            check()
                            if len(members) >= self.limits.members:
                                raise MemoryError("Memory directory limit exceeded")
                            remaining[0] -= len(os.fsencode(entry.name)) + 16
                            if remaining[0] < 0:
                                raise MemoryError("Memory evidence read limit exceeded")
                            members.append(
                                (
                                    entry.name,
                                    git_mode(entry.stat(follow_symlinks=False).st_mode),
                                )
                            )
                    digest = membership_digest(members)
                else:
                    if (
                        before.st_size > self.limits.file_bytes
                        or before.st_size > remaining[0]
                    ):
                        raise MemoryError("Memory evidence read limit exceeded")
                    hasher = hashlib.sha256()
                    size = 0
                    while block := os.read(fd, 16384):
                        check()
                        size += len(block)
                        remaining[0] -= len(block)
                        if size > self.limits.file_bytes or remaining[0] < 0:
                            raise MemoryError("Memory evidence read limit exceeded")
                        hasher.update(block)
                    digest = hasher.hexdigest()
                if _signature(before) != _signature(os.fstat(fd)) or _signature(
                    before
                ) != _signature(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    raise MemoryError("Memory evidence changed during inspection")
                return Fingerprint(
                    path=path,
                    kind=file_kind,
                    mode=stat.S_IMODE(before.st_mode),
                    sha256=digest,
                    members=len(members),
                )
            finally:
                os.close(fd)
        finally:
            os.close(parent)
