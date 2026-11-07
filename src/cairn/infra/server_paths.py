"""One host-selected layout; no component discovers production state paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    return (base / "cairn").resolve()


def overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


@dataclass(frozen=True, slots=True)
class ServerPaths:
    workspace: Path
    data_root: Path
    worktrees: Path

    def __post_init__(self) -> None:
        for name in ("workspace", "data_root", "worktrees"):
            path = getattr(self, name)
            if not path.is_absolute():
                raise ValueError(f"{name} must be absolute")
            object.__setattr__(self, name, path.resolve())
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        if overlaps(self.workspace, self.data_root):
            raise ValueError("data_root must be outside the Agent workspace")
        if overlaps(self.workspace, self.worktrees):
            raise ValueError("worktrees must be outside the Agent workspace")
        for private in self.private_roots:
            if overlaps(private, self.worktrees):
                raise ValueError("Host state must be outside Agent worktrees")
            if private.is_symlink():
                raise ValueError(f"Host state cannot use a symlink: {private}")

    @property
    def runtime(self) -> Path:
        return self.data_root / "runtime"

    @property
    def sessions(self) -> Path:
        return self.runtime / "sessions"

    @property
    def board(self) -> Path:
        return self.runtime / "board"

    @property
    def wechat(self) -> Path:
        return self.data_root / "wechat"

    @property
    def schedules(self) -> Path:
        # Preserve the deployed singular directory: no implicit data move.
        return self.data_root / "schedule"

    @property
    def agent(self) -> Path:
        return self.data_root / "agent"

    @property
    def domain(self) -> Path:
        """Authoritative Conversation/Task/Run store for P0-B2/B3."""
        return self.runtime / "domain.sqlite3"

    @property
    def token(self) -> Path:
        return self.runtime / "runtime.token"

    @property
    def private_roots(self) -> tuple[Path, ...]:
        return (self.runtime, self.wechat, self.schedules, self.agent)

    def protect_policy(self, path: Path) -> None:
        if path.is_symlink() or any(
            overlaps(path.resolve(), writable)
            for writable in (self.workspace, self.worktrees)
        ):
            raise ValueError("Host configuration must be outside Agent-writable roots")
