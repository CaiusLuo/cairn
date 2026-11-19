"""Host-private channel credentials, kept out of the shared database.

Each secret lives in its own ``0600`` file under ``<channel_root>/credentials``
and is written with an atomic rename. The database stores only the reference
and the time the current secret was created, so a database dump or console
query can never disclose a bot token.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

_REF = re.compile(r"\Acred-[0-9a-f]{32}\Z")
_TEMP_SUFFIX = ".tmp"


class CredentialVault:
    def __init__(self, root: Path) -> None:
        raw = root.expanduser()
        if raw.is_symlink():
            raise ValueError("Channel state cannot be a symlink")
        self.root = raw.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self.root.chmod(0o700)
        self.path = self.root / "credentials"
        if self.path.is_symlink():
            raise ValueError("Channel credentials cannot be a symlink")
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            self.path.chmod(0o700)

    def _file(self, ref: str) -> Path:
        if not _REF.match(ref):
            raise ValueError("invalid credential reference")
        return self.path / f"{ref}.json"

    def create(self, secret: dict[str, str], created_at: str) -> str:
        ref = "cred-" + secrets.token_hex(16)
        self._write(
            ref,
            {"created_at": created_at, "updated_at": created_at, "secret": secret},
        )
        return ref

    def replace(
        self, current: str | None, secret: dict[str, str], created_at: str
    ) -> str:
        ref = self.create(secret, created_at)
        if current is not None:
            self.delete(current)
        return ref

    def created_at(self, ref: str) -> str | None:
        payload = self._read(ref)
        value = None if payload is None else payload.get("created_at")
        return value if isinstance(value, str) else None

    def load(self, ref: str | None) -> dict[str, str] | None:
        if ref is None:
            return None
        payload = self._read(ref)
        if payload is None:
            return None
        secret = payload.get("secret")
        if not isinstance(secret, dict):
            return None
        return {
            str(key): str(item)
            for key, item in secret.items()
            if isinstance(key, str) and isinstance(item, str)
        }

    def delete(self, ref: str) -> None:
        with suppress(FileNotFoundError):
            self._file(ref).unlink()

    def _read(self, ref: str) -> dict[str, object] | None:
        try:
            raw = self._file(ref).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _write(self, ref: str, payload: dict[str, object]) -> None:
        target = self._file(ref)
        temp = target.with_name(f"{target.name}.{secrets.token_hex(8)}{_TEMP_SUFFIX}")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    payload,
                    output,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, target)
        except BaseException:
            with suppress(OSError):
                temp.unlink()
            raise


__all__ = ["CredentialVault"]
