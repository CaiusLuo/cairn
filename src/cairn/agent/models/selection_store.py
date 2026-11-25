"""Durable provider/group/model selection under the runtime state directory."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from cairn.agent.models.selection import ModelRef
from cairn.shared.atomic_json import atomic_write

_VERSION = 1


class ModelSelectionStore:
    """Atomic, private JSON state for the running selection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> ModelRef | None:
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("Model selection state cannot be a symlink")
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(raw, dict) or raw.get("version") != _VERSION:
                return None
            if "default" not in raw:
                return None
            try:
                return ModelRef.from_dict(raw["default"])
            except ValueError:
                return None

    def save(self, ref: ModelRef) -> None:
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("Model selection state cannot be a symlink")
            atomic_write(self.path, self._payload(ref))

    def encode(self, ref: ModelRef) -> bytes:
        """Bytes for a caller that is already inside a file transaction."""
        return (
            json.dumps(self._payload(ref), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _payload(ref: ModelRef) -> dict[str, object]:
        return {"version": _VERSION, "default": ref.as_dict()}
