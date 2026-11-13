"""Trusted local configuration for the read-only web surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_REFRESH_SECONDS = 2.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Host-selected roots and the loopback endpoint for one W1 process.

    Every path is resolved at startup. Browser requests never choose a root;
    the only user-controlled identifiers are a Session UUID, a TaskBoard id and
    a Clip id, all validated by their owning stores. ``knowledge_dir`` defaults
    beside the TaskBoard so existing host configurations keep one private state
    tree, but it is always forced outside the Agent workspace.
    """

    workspace: Path
    sessions_dir: Path
    tasks_dir: Path
    knowledge_dir: Path | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    web_root: Path | None = None
    username: str = "cairn"
    auth_passphrase: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).expanduser().resolve()
        sessions_dir = Path(self.sessions_dir).expanduser().resolve()
        tasks_dir = Path(self.tasks_dir).expanduser().resolve()
        knowledge_dir = (
            tasks_dir.parent / "knowledge"
            if self.knowledge_dir is None
            else Path(self.knowledge_dir).expanduser().resolve()
        )
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "sessions_dir", sessions_dir)
        object.__setattr__(self, "tasks_dir", tasks_dir)
        object.__setattr__(self, "knowledge_dir", knowledge_dir)
        if not workspace.is_dir():
            raise ValueError("--workspace must be an existing directory")
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError("web surface binds to a loopback host only")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        if self.refresh_seconds <= 0 or self.refresh_seconds > 60:
            raise ValueError("--refresh-seconds must be in (0, 60]")
        for label, state_root in (
            ("--sessions-dir", sessions_dir),
            ("--tasks-dir", tasks_dir),
            ("--knowledge-dir", knowledge_dir),
        ):
            if state_root == workspace or state_root.is_relative_to(workspace):
                raise ValueError(f"{label} must live outside --workspace")
            if workspace.is_relative_to(state_root):
                raise ValueError(f"{label} must not contain --workspace")
        if len({sessions_dir, tasks_dir, knowledge_dir}) != 3:
            raise ValueError(
                "--sessions-dir, --tasks-dir and --knowledge-dir must differ"
            )
        if self.web_root is not None:
            object.__setattr__(
                self, "web_root", Path(self.web_root).expanduser().resolve()
            )

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        host = "[::1]" if self.host == "::1" else self.host
        return (f"http://{host}:{self.port}", f"https://{host}:{self.port}")

    def origin_for(self) -> str:
        return self.allowed_origins[0]
