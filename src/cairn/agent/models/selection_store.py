"""Durable model selection, separate from deployment configuration.

The file contains only a provider/group/model reference.  It never contains a
credential or a copy of ``models.toml``, and writing it cannot change what the
host has approved.  A missing or corrupt file degrades to "no preference"; the
registry then serves the first approved model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cairn.agent.models.selection import ModelRef
from cairn.shared.atomic_json import atomic_write

DEFAULT_ROLE = "default"
_VERSION = 1


@dataclass(frozen=True)
class ModelSelections:
    default: ModelRef | None = None
    roles: tuple[tuple[str, ModelRef], ...] = ()

    def for_role(self, role: str = DEFAULT_ROLE) -> ModelRef | None:
        if role == DEFAULT_ROLE:
            return self.default
        return dict(self.roles).get(role) or self.default

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"version": _VERSION}
        if self.default is not None:
            payload["default"] = self.default.as_dict()
        if self.roles:
            payload["roles"] = {name: ref.as_dict() for name, ref in self.roles}
        return payload


class ModelSelectionStore:
    """Atomic, private JSON state under the runtime state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> ModelSelections:
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("Model selection state cannot be a symlink")
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return ModelSelections()
            if not isinstance(raw, dict) or raw.get("version") != _VERSION:
                return ModelSelections()
            try:
                default = (
                    ModelRef.from_dict(raw["default"]) if "default" in raw else None
                )
                roles: list[tuple[str, ModelRef]] = []
                role_values = raw.get("roles", {})
                if not isinstance(role_values, dict):
                    return ModelSelections()
                for name, value in role_values.items():
                    if not isinstance(name, str) or name == DEFAULT_ROLE:
                        return ModelSelections()
                    roles.append((name, ModelRef.from_dict(value)))
            except ValueError:
                return ModelSelections()
            return ModelSelections(default=default, roles=tuple(roles))

    def save(self, ref: ModelRef, role: str = DEFAULT_ROLE) -> None:
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("Model selection state cannot be a symlink")
            current = self.load()
            next_value = self._with_ref(current, ref, role)
            atomic_write(self.path, next_value.as_dict())

    def encode(self, ref: ModelRef, role: str = DEFAULT_ROLE) -> bytes:
        """Bytes for a caller that is already inside a file transaction."""
        with self._lock:
            current = self.load()
            payload = self._with_ref(current, ref, role).as_dict()
        return (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _with_ref(
        selections: ModelSelections, ref: ModelRef, role: str
    ) -> ModelSelections:
        if role == DEFAULT_ROLE:
            return ModelSelections(default=ref, roles=selections.roles)
        roles = dict(selections.roles)
        roles[role] = ref
        return ModelSelections(
            default=selections.default,
            roles=tuple(sorted(roles.items())),
        )

    def snapshot(self) -> Mapping[str, object]:
        """Secret-free view for diagnostics and tests."""
        return self.load().as_dict()
