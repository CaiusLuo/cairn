"""Host-private Markdown storage for one personal Agent.

The repository's Project Memory is evidence-backed and human-approved; this
store is deliberately separate: AGENT.md, MEMORY.md and daily notes belong to
the personal Agent and live outside every Agent-writable Workspace.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

AGENT_HOME_ENV = "CAIRN_AGENT_HOME"
DEFAULT_AGENT_MD = """# Personal AGENT

This file belongs to this personal Agent, not the project repository.
Project AGENTS.md, system rules and permissions take precedence.

## Preferences
## Learned
"""
DEFAULT_MEMORY_MD = "# Long-term Memory\n"


def default_agent_home() -> Path:
    override = os.environ.get(AGENT_HOME_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "agent"


def ensure_outside_workspace(state_root: Path, workspace: Path) -> None:
    resolved_state = state_root.resolve()
    resolved_workspace = workspace.resolve()
    if resolved_state == resolved_workspace or resolved_state.is_relative_to(
        resolved_workspace
    ):
        raise ValueError(
            "Personal agent memory directory must live outside the Agent-writable Workspace"
        )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    with suppress(OSError):
        path.chmod(0o600)


class AgentMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.agent_md_path = root / "AGENT.md"
        self.memory_md_path = root / "MEMORY.md"
        self.memory_dir = root / "memory"
        self.audit_path = root / "memory" / "AGENT_CHANGES.md"
        self.proposal_path = root / "pending_agent_update.json"

    def ensure_initialized(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        if not self.agent_md_path.exists():
            _atomic_write(self.agent_md_path, DEFAULT_AGENT_MD)
        if not self.memory_md_path.exists():
            _atomic_write(self.memory_md_path, DEFAULT_MEMORY_MD)

    def read_agent_md(self) -> str:
        return self._read(self.agent_md_path)

    def read_memory_md(self) -> str:
        return self._read(self.memory_md_path)

    def append_memory_md(self, text: str) -> None:
        self.ensure_initialized()
        _append(self.memory_md_path, text if text.endswith("\n") else text + "\n")

    def _read(self, path: Path, *, limit: int | None = None) -> str:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                text = handle.read()
        except FileNotFoundError:
            return ""
        except OSError:
            raise ValueError(f"agent memory file is unreadable: {path.name}") from None
        return text if limit is None else text[:limit]

    def daily_path(self, day: date) -> Path:
        return self.memory_dir / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y_%m_%d}.md"

    def read_daily(self, day: date, *, limit: int | None = None) -> str:
        return self._read(self.daily_path(day), limit=limit)

    def read_daily_tail(self, day: date, *, max_chars: int) -> str:
        text = self._read(self.daily_path(day))
        return text if len(text) <= max_chars else text[-max_chars:]

    def append_daily(self, day: date, text: str) -> None:
        path = self.daily_path(day)
        if not path.exists():
            _atomic_write(path, f"# {day.isoformat()}\n\n")
        _append(path, text if text.endswith("\n") else text + "\n")

    def recent_daily(
        self, now: datetime, days: int = 2, *, max_chars: int = 20000
    ) -> tuple[tuple[str, str], ...]:
        result: list[tuple[str, str]] = []
        for delta in range(days):
            day = now.date() - timedelta(days=delta)
            body = self.read_daily_tail(day, max_chars=max_chars)
            if body:
                result.append((day.isoformat(), body))
        return tuple(result)

    def list_daily_days(self) -> tuple[date, ...]:
        found: list[date] = []
        for path in sorted(self.memory_dir.glob("*/*/*.md")):
            if path.name == "AGENT_CHANGES.md":
                continue
            match = re.search(r"(\d{4}_\d{2}_\d{2})\.md$", path.name)
            if not match:
                continue
            try:
                found.append(datetime.strptime(match.group(1), "%Y_%m_%d").date())
            except ValueError:
                continue
        return tuple(sorted(set(found)))

    def snapshot_agent_md(self, now: datetime) -> Path:
        history = self.memory_dir / "AGENT_HISTORY"
        target = history / f"{now:%Y%m%d_%H%M%S_%f}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            target.parent.chmod(0o700)
        _atomic_write(target, self.read_agent_md())
        return target

    def write_agent_md(
        self, text: str, *, snapshot: bool = True, now: datetime | None = None
    ) -> None:
        self.ensure_initialized()
        if snapshot:
            self.snapshot_agent_md(now or datetime.now().astimezone())
        _atomic_write(self.agent_md_path, text)

    def rollback_agent_md(self) -> bool:
        history = self.memory_dir / "AGENT_HISTORY"
        backups = sorted(history.glob("*.md")) if history.exists() else []
        if not backups:
            return False
        previous = self._read(backups[-1])
        _atomic_write(self.agent_md_path, previous)
        backups[-1].unlink(missing_ok=True)
        return True

    def append_audit(self, text: str) -> None:
        _append(self.audit_path, text if text.endswith("\n") else text + "\n")

    def save_proposal(self, payload: dict[str, Any]) -> None:
        _atomic_write(
            self.proposal_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def load_proposal(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            raise ValueError("pending AGENT proposal is unreadable") from None
        return raw if isinstance(raw, dict) else None

    def clear_proposal(self) -> None:
        with suppress(FileNotFoundError):
            self.proposal_path.unlink()
