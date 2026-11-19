"""Private storage for files attached to one conversation turn.

An upload exists before its Run does, so the Web client stages it here first.
Admission then claims the staged files into the Run's own directory; the model
sees them only through a Run-scoped tool, never through workspace paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_ATTACHMENT_TOTAL_BYTES = 25 * 1024 * 1024
# An upload that is never sent must not accumulate forever.
UPLOAD_TTL_SECONDS = 24 * 60 * 60

_RUN_ID = re.compile(r"run-[0-9a-f]{32}")
_ATTACHMENT_ID = re.compile(r"^att-[0-9a-f]{32}$")
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")


def safe_attachment_name(raw: str) -> str:
    """Keep the readable basename, never a path, and always a real name."""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE_NAME.sub("_", base).strip(" .")
    if len(base.encode()) > 240:
        stem, dot, suffix = base.rpartition(".")
        extension = dot + suffix if stem and len(suffix.encode()) <= 16 else ""
        stem = stem if extension else base
        base = (
            stem.encode()[: 240 - len(extension.encode())]
            .decode("utf-8", errors="ignore")
            .rstrip(" .")
            + extension
        )
    return base or "attachment"


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class AttachmentUpload:
    attachment_id: str
    name: str
    media_type: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AttachmentPlan:
    """One staged file claimed by an admitted Run."""

    attachment_id: str
    name: str
    media_type: str
    size: int
    sha256: str


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.uploads = self.root / "uploads"
        self.runs = self.root / "runs"
        self._next_sweep = 0.0
        for directory in (self.root, self.uploads, self.runs):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    def stage(self, name: str, media_type: str, data: bytes) -> AttachmentUpload:
        if not data:
            raise ValueError("Attachment is empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment exceeds the per-file limit")
        if time.monotonic() >= self._next_sweep:
            self.sweep()
        attachment_id = "att-" + uuid4().hex
        cleaned = safe_attachment_name(name)
        media = (media_type or "application/octet-stream").split(";")[0].strip().lower()
        digest = hashlib.sha256(data).hexdigest()
        _write_private(self.uploads / attachment_id, data)
        _write_private(
            self.uploads / (attachment_id + ".json"),
            json.dumps(
                {
                    "attachment_id": attachment_id,
                    "name": cleaned,
                    "media_type": media[:200] or "application/octet-stream",
                    "size": len(data),
                    "sha256": digest,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        return AttachmentUpload(
            attachment_id=attachment_id,
            name=cleaned,
            media_type=media or "application/octet-stream",
            size=len(data),
            sha256=digest,
        )

    def plan(
        self, attachment_ids: tuple[str, ...], *, admitted: bool = False
    ) -> tuple[AttachmentPlan, ...]:
        """Validate and name every upload before a Run is admitted."""
        if len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError("Too many attachments for one message")
        planned: list[AttachmentPlan] = []
        # The manifest is host metadata, never an attachable file name.
        used: set[str] = {"manifest.json"}
        total = 0
        for attachment_id in dict.fromkeys(attachment_ids):
            if not _ATTACHMENT_ID.match(attachment_id):
                raise ValueError("Unknown attachment")
            metadata = self._metadata(attachment_id, admitted=admitted)
            name = _unique_name(safe_attachment_name(metadata.name), used)
            total += metadata.size
            if total > MAX_ATTACHMENT_TOTAL_BYTES:
                raise ValueError("Attachments exceed the message limit")
            planned.append(
                AttachmentPlan(
                    attachment_id=attachment_id,
                    name=name,
                    media_type=metadata.media_type,
                    size=metadata.size,
                    sha256=metadata.sha256,
                )
            )
        return tuple(planned)

    def bind(self, run_id: str, plans: tuple[AttachmentPlan, ...]) -> None:
        """Claim staged files into the Run directory; the caller already owns the Run."""
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir.chmod(0o700)
        # Persist the full plan before claiming the first file, so a restart can
        # finish a partial claim with the original names and upload identities.
        _write_private(
            run_dir / "manifest.json",
            json.dumps([asdict(plan) for plan in plans], ensure_ascii=False).encode(),
        )
        for plan in plans:
            source = self.uploads / plan.attachment_id
            target = run_dir / plan.name
            if target.exists():
                if (
                    source.exists()
                    or not target.is_file()
                    or target.is_symlink()
                    or target.stat().st_size != plan.size
                    or hashlib.sha256(target.read_bytes()).hexdigest() != plan.sha256
                ):
                    raise ValueError("Attachment name collides in this Run")
            elif source.is_file() and not source.is_symlink():
                os.replace(source, target)
            else:
                raise ValueError("Attachment is no longer available")
            (self.uploads / (plan.attachment_id + ".json")).unlink(missing_ok=True)

    def prepared_claim(
        self, run_id: str, attachment_ids: tuple[str, ...]
    ) -> tuple[AttachmentPlan, ...]:
        run_dir = self.runs / run_id
        rows = self._manifest_rows(run_dir)
        if rows is None or len(rows) != len(set(attachment_ids)):
            return ()
        plans: list[AttachmentPlan] = []
        for attachment_id, row in zip(dict.fromkeys(attachment_ids), rows, strict=True):
            values = self._manifest_entry(row)
            if (
                values is None
                or not isinstance(row, dict)
                or row.get("attachment_id") != attachment_id
                or not _ATTACHMENT_ID.fullmatch(attachment_id)
            ):
                return ()
            plans.append(AttachmentPlan(attachment_id, *values))
        return tuple(plans)

    @staticmethod
    def _manifest_rows(run_dir: Path) -> list[object] | None:
        try:
            entries = json.loads((run_dir / "manifest.json").read_bytes())
        except (OSError, ValueError):
            return None
        return entries if isinstance(entries, list) else None

    @staticmethod
    def _manifest_entry(row: object) -> tuple[str, str, int, str] | None:
        if not isinstance(row, dict):
            return None
        name, media_type, size, sha256 = (
            row.get(field) for field in ("name", "media_type", "size", "sha256")
        )
        if (
            not isinstance(name, str)
            or name in {"", ".", "..", "manifest.json"}
            or len(name.encode()) > 255
            or _UNSAFE_NAME.search(name)
            or not isinstance(media_type, str)
            or type(size) is not int
            or not 1 <= size <= MAX_ATTACHMENT_BYTES
            or not isinstance(sha256, str)
        ):
            return None
        return name, media_type, size, sha256

    def manifest(self, run_id: str) -> list[dict[str, object]] | None:
        """Read a completed claim's manifest, for recovery after a crash."""
        run_dir = self.runs / run_id
        entries = self._manifest_rows(run_dir)
        if entries is None:
            return None
        recovered: list[dict[str, object]] = []
        for entry in entries:
            values = self._manifest_entry(entry)
            if values is None:
                return None
            name, media_type, size, sha256 = values
            payload = run_dir / name
            if (
                payload.is_symlink()
                or not payload.is_file()
                or payload.stat().st_size != size
            ):
                return None
            recovered.append(
                {"name": name, "media_type": media_type, "size": size, "sha256": sha256}
            )
        return recovered

    def discard(self, run_id: str) -> None:
        """Remove a deleted Run's private files; never leave a Run directory."""
        directory = self.runs / run_id
        if directory.parent == self.runs:
            shutil.rmtree(directory, ignore_errors=True)

    def sweep_orphan_runs(self, existing: set[str]) -> None:
        for directory in self.runs.iterdir():
            if _RUN_ID.fullmatch(directory.name) and directory.name not in existing:
                self.discard(directory.name)

    def sweep(self) -> None:
        cutoff = time.time() - UPLOAD_TTL_SECONDS
        for path in self.uploads.iterdir():
            is_metadata = path.name.endswith(".json")
            stem = path.name.removesuffix(".json")
            if not is_metadata and not _ATTACHMENT_ID.match(path.name):
                continue
            if _ATTACHMENT_ID.match(stem) and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        self._next_sweep = time.monotonic() + 3600

    def _metadata(self, attachment_id: str, *, admitted: bool) -> AttachmentPlan:
        path = self.uploads / (attachment_id + ".json")
        payload = self.uploads / attachment_id
        try:
            if (
                not admitted
                and payload.stat().st_mtime < time.time() - UPLOAD_TTL_SECONDS
            ):
                raise ValueError("Attachment expired")
            metadata = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise ValueError("Attachment is no longer available") from None
        if (
            payload.is_symlink()
            or not payload.is_file()
            or not isinstance(metadata, dict)
        ):
            raise ValueError("Attachment is no longer available")
        name = metadata.get("name")
        media_type = metadata.get("media_type")
        size = metadata.get("size")
        sha256 = metadata.get("sha256")
        if (
            metadata.get("attachment_id") != attachment_id
            or not isinstance(name, str)
            or not isinstance(media_type, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(sha256, str)
        ):
            raise ValueError("Attachment metadata is invalid")
        return AttachmentPlan(
            attachment_id=attachment_id,
            name=name,
            media_type=media_type,
            size=size,
            sha256=sha256,
        )


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    index = 2
    while candidate in used:
        candidate = f"{stem}-{index}{dot}{suffix}"
        index += 1
    used.add(candidate)
    return candidate
