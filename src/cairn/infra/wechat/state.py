"""Host-private WeChat account/state, outside every Agent-writable workspace.

The bot token, poll cursor and per-message facts live here. Files are 0600 in a
0700 directory; the default is ``$XDG_STATE_HOME/cairn/wechat`` (usually
``~/.local/state/cairn/wechat``). Nothing here is ever put in the repository, a
Workspace, a trace or a model request.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "CAIRN_WECHAT_STATE_DIR"
STATE_VERSION = 1


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "wechat"


def ensure_outside_workspace(state_root: Path, workspace: Path) -> None:
    """Refuse a host state directory the Agent could reach through the Workspace."""
    resolved_state = state_root.resolve()
    resolved_workspace = workspace.resolve()
    if resolved_state == resolved_workspace or resolved_state.is_relative_to(
        resolved_workspace
    ):
        raise ValueError(
            "WeChat state directory must live outside the Agent-writable Workspace"
        )


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


@dataclass(frozen=True, slots=True)
class WechatAccount:
    bot_token: str
    ilink_bot_id: str
    base_url: str
    ilink_user_id: str = ""


class WechatState:
    """Single-account durable facts for login, polling and task admission."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.account_path = root / "account.json"
        self.state_path = root / "state.json"
        self._state: dict[str, Any] | None = None

    # -- account ---------------------------------------------------------
    def load_account(self) -> WechatAccount | None:
        try:
            raw = json.loads(self.account_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            raise ValueError("WeChat account state is unreadable") from None
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise ValueError("WeChat account state has an unsupported shape/version")
        try:
            return WechatAccount(
                bot_token=str(raw["bot_token"]),
                ilink_bot_id=str(raw["ilink_bot_id"]),
                base_url=str(raw["base_url"]),
                ilink_user_id=str(raw.get("ilink_user_id", "")),
            )
        except KeyError:
            raise ValueError("WeChat account state is incomplete") from None

    def save_account(self, account: WechatAccount) -> None:
        _atomic_write(
            self.account_path,
            {
                "version": STATE_VERSION,
                "bot_token": account.bot_token,
                "ilink_bot_id": account.ilink_bot_id,
                "base_url": account.base_url,
                "ilink_user_id": account.ilink_user_id,
            },
        )

    # -- poll/task state -------------------------------------------------
    def load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # First run: initialise a valid empty state. Only an *existing*
            # damaged file is a fail-closed error.
            self._state = {
                "version": STATE_VERSION,
                "cursor": "",
                "messages": {},
            }
            return self._state
        except (OSError, json.JSONDecodeError):
            raise ValueError("WeChat runtime state is unreadable") from None
        if not isinstance(raw, dict):
            raise ValueError("WeChat runtime state is malformed")
        if raw.get("version") != STATE_VERSION:
            raise ValueError("WeChat runtime state has an unsupported version")
        cursor = raw.get("cursor")
        messages = raw.get("messages")
        if not isinstance(cursor, str) or not isinstance(messages, dict):
            # A damaged state file must fail closed: silently starting from an
            # empty dedupe map could re-execute an already-started task.
            raise ValueError("WeChat runtime state is damaged")
        self._state = {
            "version": STATE_VERSION,
            "cursor": cursor,
            "messages": messages,
        }
        return self._state

    def flush(self) -> None:
        state = self.load()
        _atomic_write(self.state_path, state)

    def get_cursor(self) -> str:
        return str(self.load().get("cursor", ""))

    def set_cursor(self, cursor: str) -> None:
        self.load()["cursor"] = cursor
        self.flush()

    def latest_context(self, sender: str) -> str | None:
        """Latest reply context for a sender, for owner-directed delivery."""
        latest: tuple[float, str] | None = None
        for record in self.load().get("messages", {}).values():
            if not isinstance(record, dict) or record.get("sender") != sender:
                continue
            context = record.get("context_token")
            updated = record.get("updated")
            if isinstance(context, str) and context:
                stamp = float(updated) if isinstance(updated, (int, float)) else 0.0
                if latest is None or stamp >= latest[0]:
                    latest = (stamp, context)
        return latest[1] if latest is not None else None

    def get_record(self, key: str) -> dict[str, Any] | None:
        record = self.load()["messages"].get(key)
        return record if isinstance(record, dict) else None

    def record_received(
        self,
        key: str,
        *,
        sender: str,
        context_token: str,
        text: str,
        stable: bool = True,
    ) -> bool:
        """Persist a new received fact; False when the key already exists."""
        messages = self.load()["messages"]
        if key in messages:
            return False
        messages[key] = {
            "state": "received",
            "sender": sender,
            "context_token": context_token,
            "text": text,
            "stable": stable,
            "updated": time.time(),
        }
        self.flush()
        return True

    def mark(self, key: str, state: str, **fields: Any) -> None:
        messages = self.load()["messages"]
        record = messages.get(key, {})
        if not isinstance(record, dict):
            record = {}
        record["state"] = state
        record["updated"] = time.time()
        record.update(fields)
        messages[key] = record
        self.flush()

    def records_in_state(self, state: str) -> list[tuple[str, dict[str, Any]]]:
        messages = self.load()["messages"]
        return [
            (key, record)
            for key, record in messages.items()
            if isinstance(record, dict) and record.get("state") == state
        ]


class WechatLockError(RuntimeError):
    """Another serve process already owns this account state."""


class WechatServeLock:
    """Host-side exclusive lock held for one serve process lifetime."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "serve.lock"
        self._fd: int | None = None

    def acquire(self, *, description: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.state_dir.chmod(0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WechatLockError(
                "another WeChat serve process already owns this account/workspace"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, (description + "\n").encode("utf-8"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        with suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(self._fd)
        self._fd = None
