"""Private local paths and a stable, process-lifetime writer lock."""

import ctypes
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import get_ident

from cairn.infra.sessions.models import MAX_DATABASE_BYTES, SessionStoreError

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _local_filesystem(fd: int) -> None:
    """Fail closed on unknown/nonlocal VFS. No host processes are dispatched.

    Darwin statfs64 flags/layout are from sys/mount.h; Linux statfs starts
    with f_type (native long). Restrict Linux to known local filesystem magic.
    WAL/flock correctness on any deployment still requires host validation.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    result = ctypes.create_string_buffer(4096)
    if sys.platform == "darwin":
        probe = libc.fstatfs64
        probe.argtypes = [ctypes.c_int, ctypes.c_void_p]
        probe.restype = ctypes.c_int
        if probe(fd, result) != 0 or not (
            ctypes.c_uint32.from_buffer(result, 64).value & 0x1000  # MNT_LOCAL
        ):
            raise SessionStoreError("unsupported_filesystem")
    elif sys.platform == "linux":
        probe = libc.fstatfs
        probe.argtypes = [ctypes.c_int, ctypes.c_void_p]
        probe.restype = ctypes.c_int
        magic = {
            0xEF53,
            0x58465342,
            0x9123683E,
            0x01021994,
            0x794C7630,
            0x2FC12FC1,
            0xF2F52010,
        }  # ext, XFS, Btrfs, tmpfs, overlay, ZFS, F2FS
        if (
            probe(fd, result) != 0
            or (ctypes.c_long.from_buffer(result).value & 0xFFFFFFFF) not in magic
        ):
            raise SessionStoreError("unsupported_filesystem")
    else:
        raise SessionStoreError("unsupported_locking")


class StorePaths:
    """Own directory/lock descriptors; SQLite alone owns existing DB descriptors.

    Closing a raw DB descriptor can drop another connection's POSIX locks.
    Only exclusive creation opens one, synced/closed before SQLite connects.
    Same-user hostile path racing is outside the trusted-host contract.
    """

    def __init__(
        self, directory: Path, forbidden_roots: tuple[Path, ...], *, writer: bool
    ) -> None:
        self.directory = directory.absolute()
        self.path = self.directory / "sessions.sqlite3"
        self.lock_path = self.directory / "writer.lock"
        self.access_path = self.directory / "inspection.lock"
        self.writer = writer
        self.directory_fd = -1
        self.lock_fd = -1
        self.access_fd = -1
        self.new_database = False
        self._components: dict[Path, tuple[int, int]] = {}
        self._files: dict[Path, tuple[int, int]] = {}
        self._read_sidecars: dict[Path, tuple[int, int]] | None = None
        self._pid = os.getpid()
        self._thread = get_ident()
        try:
            if not forbidden_roots:
                raise SessionStoreError("forbidden_roots_required")
            self._forbidden = tuple(
                (p.absolute(), p.resolve()) for p in forbidden_roots
            )
            roots = tuple(target for _, target in self._forbidden)
            canonical = self.directory.resolve()
            if any(
                canonical.is_relative_to(p) or p.is_relative_to(canonical)
                for p in roots
            ):
                raise SessionStoreError("writable_root_overlap")
            if any(p in (".", "..") for p in directory.parts):
                raise SessionStoreError("unsafe_path")
            self._open_directory(create=writer)
            _local_filesystem(self.directory_fd)
            self.check()
            if writer:
                self._lock()
                try:
                    fd = os.open(
                        self.path.name,
                        _FILE | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=self.directory_fd,
                    )
                except FileExistsError:
                    pass
                else:
                    try:
                        self._files[self.path] = _identity(os.fstat(fd))
                        os.fsync(fd)
                        self.new_database = True
                    finally:
                        os.close(fd)
            self.access_fd = os.open(
                self.access_path.name,
                (os.O_RDWR | os.O_CREAT if writer else os.O_RDONLY)
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=self.directory_fd,
            )
            self.check()
            if self.path not in self._files:
                raise SessionStoreError("database_missing")
        except SessionStoreError:
            self.close()
            raise
        except (OSError, AttributeError, ValueError, RuntimeError):
            self.close()
            raise SessionStoreError("unsafe_path_or_lock") from None

    def _open_directory(self, *, create: bool) -> None:
        fd = os.open(self.directory.anchor, _DIRECTORY)
        current = Path(self.directory.anchor)
        self._components[current] = _identity(os.fstat(fd))
        try:
            for name in self.directory.parts[1:]:
                current /= name
                try:
                    next_fd = os.open(name, _DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(name, 0o700, dir_fd=fd)
                    os.fsync(fd)  # Synchronize every newly created parent entry.
                    next_fd = os.open(name, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                self._components[current] = _identity(os.fstat(fd))
            self.directory_fd = fd
        except BaseException:
            os.close(fd)
            raise

    def _lock(self) -> None:
        import fcntl

        self.lock_fd = os.open(
            self.lock_path.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        self.check()
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SessionStoreError("writer_busy") from None
        except OSError:
            raise SessionStoreError("unsupported_locking") from None

    def check(self) -> None:
        if self.directory_fd < 0 or os.getpid() != self._pid:
            raise SessionStoreError("closed_or_forked_store")
        if get_ident() != self._thread:
            raise SessionStoreError("wrong_store_thread")
        try:
            if any(path.resolve() != target for path, target in self._forbidden):
                raise SessionStoreError("forbidden_root_changed")
        except (OSError, RuntimeError):
            raise SessionStoreError("forbidden_root_changed") from None
        for path, expected in self._components.items():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or _identity(info) != expected:
                raise SessionStoreError("path_identity_changed")
        directory = os.fstat(self.directory_fd)
        if (
            _identity(directory) != self._components[self.directory]
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise SessionStoreError("private_directory_required")
        for path in (
            self.path,
            self.lock_path,
            self.access_path,
            *(Path(str(self.path) + s) for s in ("-wal", "-shm", "-journal")),
        ):
            tracked: dict[Path, tuple[int, int]] | None = self._files
            if not self.writer and path not in (
                self.path,
                self.lock_path,
                self.access_path,
            ):
                # Sidecars can disappear/reappear between inspection calls as
                # a writer closes/reopens. Pin them only under the shared guard.
                tracked = self._read_sidecars
            try:
                info = os.stat(
                    path.name, dir_fd=self.directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                if tracked is not None and path in tracked:
                    # SQLite removes sidecars when the final connection closes.
                    # Disappearance inside one guarded read is never expected.
                    raise SessionStoreError("path_identity_changed") from None
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise SessionStoreError("unsafe_store_file")
            if info.st_size > MAX_DATABASE_BYTES * (1 if path == self.path else 2):
                raise SessionStoreError("database_limit")
            if tracked is not None:
                old = tracked.setdefault(path, _identity(info))
                if old != _identity(info):
                    raise SessionStoreError("path_identity_changed")
        for fd, path in (
            (self.lock_fd, self.lock_path),
            (self.access_fd, self.access_path),
        ):
            if fd >= 0 and _identity(os.fstat(fd)) != self._files.get(path):
                raise SessionStoreError("path_identity_changed")

    @contextmanager
    def access(self, *, validate: bool = True) -> Iterator[None]:
        """Pin DB/WAL during a read; serialize SQLite effects against inspectors.

        Inspectors open this existing descriptor read-only and take only SH.
        Writers retain their independent lifetime EX writer.lock. No lock waits,
        upgrades, creation by readers or writable SQLite fallback are allowed.
        Closing SQLite still uses the pinned guard after a path fault.
        """
        import fcntl

        if self.access_fd < 0 or os.getpid() != self._pid:
            raise SessionStoreError("closed_or_forked_store")
        if get_ident() != self._thread:
            raise SessionStoreError("wrong_store_thread")
        if validate:
            self.check()
        try:
            fcntl.flock(
                self.access_fd,
                (fcntl.LOCK_EX if self.writer else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            raise SessionStoreError("store_busy") from None
        except OSError:
            raise SessionStoreError("unsupported_locking") from None
        try:
            if not self.writer:
                self._read_sidecars = {}
            if validate:
                self.check()
            yield
        finally:
            self._read_sidecars = None
            fcntl.flock(self.access_fd, fcntl.LOCK_UN)

    def sync_creation(self) -> None:
        self.check()
        # SQLite FULL owns DB/WAL content synchronization. Do not open/close a
        # second DB descriptor while any SQLite connection may hold locks.
        os.fsync(self.directory_fd)  # DB/WAL/lock entries, after SQLite FULL COMMIT.

    def close(self) -> None:
        if os.getpid() != self._pid:
            raise SessionStoreError("closed_or_forked_store")
        if get_ident() != self._thread:
            raise SessionStoreError("wrong_store_thread")
        # Do not unlink writer.lock: replacing its inode would split the lock.
        for name in ("access_fd", "lock_fd", "directory_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                with suppress(OSError):
                    os.close(fd)
                setattr(self, name, -1)
