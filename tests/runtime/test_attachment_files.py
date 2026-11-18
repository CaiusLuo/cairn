"""Attachment parsing limits and storage hygiene."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

from cairn.agent.tools import attachments as tools
from cairn.agent.tools.attachments import MAX_ATTACHMENT_CHARS, ReadAttachmentTool
from cairn.conversation import attachments as storage
from cairn.conversation.attachments import AttachmentStore

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class _Ref:
    name: str
    media_type: str
    size: int


def _tool(
    tmp_path: Path, name: str, media_type: str, data: bytes
) -> ReadAttachmentTool:
    root = tmp_path / "run"
    root.mkdir()
    (root / name).write_bytes(data)
    return ReadAttachmentTool(root, (_Ref(name, media_type, len(data)),))


def _pdf_pages(lines: list[str]) -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids: list[str] = []
    for index, line in enumerate(lines):
        stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
        page_id = 4 + index * 2
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(lines)} >>".encode()
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)


def test_text_is_truncated_with_a_notice(tmp_path: Path) -> None:
    tool = _tool(
        tmp_path, "long.txt", "text/plain", b"x" * (MAX_ATTACHMENT_CHARS + 100)
    )
    result = asyncio.run(tool.execute({"name": "long.txt"}))
    assert result.stdout.startswith("x" * 100)
    assert result.stdout.endswith(
        f"[attachment truncated after {MAX_ATTACHMENT_CHARS} characters]"
    )
    assert len(result.stdout) < MAX_ATTACHMENT_CHARS + 200


def test_pdf_page_cap_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_PDF_PAGES", 1)
    tool = _tool(tmp_path, "long.pdf", "application/pdf", _pdf_pages(["one", "two"]))
    result = asyncio.run(tool.execute({"name": "long.pdf"}))
    assert "one" in result.stdout
    assert "two" not in result.stdout
    assert "[only the first 1 of 2 pages were read]" in result.stdout


def test_workbook_row_cap_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_SHEET_ROWS", 2)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in ("one", "two", "three", "four"):
        sheet.append([row])
    buffer = BytesIO()
    workbook.save(buffer)
    tool = _tool(tmp_path, "big.xlsx", XLSX_MEDIA, buffer.getvalue())
    result = asyncio.run(tool.execute({"name": "big.xlsx"}))
    assert "one" in result.stdout
    assert "four" not in result.stdout
    assert "[workbook truncated]" in result.stdout


def test_unparseable_office_file_reports_instead_of_raising(tmp_path: Path) -> None:
    tool = _tool(tmp_path, "broken.docx", DOCX_MEDIA, b"not a zip archive")
    result = asyncio.run(tool.execute({"name": "broken.docx"}))
    assert "broken.docx could not be parsed as text" in result.stdout


def test_office_expansion_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_UNCOMPRESSED_BYTES", 10)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", b"x" * 100)
    tool = _tool(tmp_path, "bomb.docx", DOCX_MEDIA, buffer.getvalue())
    result = asyncio.run(tool.execute({"name": "bomb.docx"}))
    assert "expands beyond the reading limit" in result.stdout


def test_legacy_office_formats_get_a_conversion_hint(tmp_path: Path) -> None:
    tool = _tool(tmp_path, "old.doc", "application/msword", b"\xd0\xcf\x11\xe0 legacy")
    result = asyncio.run(tool.execute({"name": "old.doc"}))
    assert "Save it as .docx" in result.stdout


def test_per_file_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "MAX_ATTACHMENT_BYTES", 4)
    store = AttachmentStore(tmp_path / "attachments")
    with pytest.raises(ValueError, match="per-file limit"):
        store.stage("big.txt", "text/plain", b"12345")


def test_total_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "MAX_ATTACHMENT_TOTAL_BYTES", 5)
    store = AttachmentStore(tmp_path / "attachments")
    first = store.stage("a.txt", "text/plain", b"123")
    second = store.stage("b.txt", "text/plain", b"456")
    with pytest.raises(ValueError, match="message limit"):
        store.plan((first.attachment_id, second.attachment_id))


def test_sweep_removes_only_expired_uploads(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    old = store.stage("old.txt", "text/plain", b"old")
    fresh = store.stage("fresh.txt", "text/plain", b"fresh")
    stale = time.time() - storage.UPLOAD_TTL_SECONDS - 60
    os.utime(store.uploads / old.attachment_id, (stale, stale))
    os.utime(store.uploads / (old.attachment_id + ".json"), (stale, stale))
    store.sweep()
    assert not (store.uploads / old.attachment_id).exists()
    assert not (store.uploads / (old.attachment_id + ".json")).exists()
    assert (store.uploads / fresh.attachment_id).exists()
    assert (store.uploads / (fresh.attachment_id + ".json")).exists()


def test_claim_manifest_requires_its_files(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    upload = store.stage("notes.txt", "text/plain", b"claim me")
    store.bind("run-1", store.plan((upload.attachment_id,)))
    assert store.manifest("run-1") is not None
    (store.runs / "run-1" / "notes.txt").unlink()
    assert store.manifest("run-1") is None
