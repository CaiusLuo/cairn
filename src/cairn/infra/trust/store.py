"""Host-private, revocable workspace trust memory.

Trust is a local consent fact, not authentication. Only the exact canonical
workspace identity (path + device + inode) is remembered; shell/network/MCP
grants are deliberately not stored here.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairn.memory.store import default_agent_home

TRUST_VERSION = 1
TRUST_FILENAME = "trusted_workspaces.json"


class TrustStoreError(RuntimeError):
    """The trust memory is unreadable or damaged; callers must fail closed."""


class TrustedWorkspaceStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else default_agent_home()
        self.path = self.root / TRUST_FILENAME
        self.lock_path = self.root / "trusted_workspaces.lock"

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise TrustStoreError("trusted workspace store is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("version") != TRUST_VERSION:
            raise TrustStoreError("trusted workspace store has an unsupported shape")
        workspaces = raw.get("workspaces")
        if not isinstance(workspaces, list):
            raise TrustStoreError("trusted workspace store has an invalid list")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in workspaces:
            if not isinstance(item, dict):
                raise TrustStoreError("trusted workspace record is damaged")
            path = item.get("path")
            device = item.get("device")
            inode = item.get("inode")
            approved = item.get("approved_at")
            if (
                not isinstance(path, str)
                or not path
                or type(device) is not int
                or type(inode) is not int
                or not isinstance(approved, str)
                or path in seen
            ):
                raise TrustStoreError("trusted workspace record is damaged")
            seen.add(path)
            result.append(
                {
                    "path": path,
                    "device": device,
                    "inode": inode,
                    "approved_at": approved,
                }
            )
        return result

    @staticmethod
    def _identity(workspace: Path) -> tuple[str, int, int]:
        resolved = workspace.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("trusted workspace is not a directory")
        info = resolved.stat()
        return str(resolved), info.st_dev, info.st_ino

    def is_trusted(self, workspace: Path) -> bool:
        try:
            path, device, inode = self._identity(workspace)
        except (OSError, ValueError):
            return False
        for item in self._load():
            if (
                item["path"] == path
                and item["device"] == device
                and item["inode"] == inode
            ):
                return True
        return False

    def remember(self, workspace: Path) -> None:
        path, device, inode = self._identity(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.root.chmod(0o700)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            with suppress(OSError):
                self.lock_path.chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records = [item for item in self._load() if item["path"] != path]
                records.append(
                    {
                        "path": path,
                        "device": device,
                        "inode": inode,
                        "approved_at": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )
                self._write(records)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def revoke(self, workspace: Path) -> bool:
        path, _, _ = self._identity(workspace)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records = self._load()
                kept = [item for item in records if item["path"] != path]
                if len(kept) == len(records):
                    return False
                self._write(kept)
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, records: list[dict[str, Any]]) -> None:
        payload = {"version": TRUST_VERSION, "workspaces": records}
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise
