"""Host-private project write lease shared by background workers and chat tools."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class WorkspaceLease:
    """Non-blocking exclusive lease over one workspace.

    The lock file lives in host-private state (never in the Workspace). A
    separate process or Agent can therefore ask whether a task currently owns
    write authority without ever writing a lock into the repository.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        try:
            self.path.chmod(0o600)
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
