"""Run-scoped access to files the host attached to one conversation turn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from html.parser import HTMLParser
from io import BytesIO
from itertools import islice
from pathlib import Path
from typing import Any, Protocol
from zipfile import ZipFile

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

from cairn.agent.models.messages import ToolResult
from cairn.agent.tools.arguments import function_schema, reject_unknown_keys
from cairn.agent.tools.base import InvalidArguments, ToolExecutionContext

MAX_ATTACHMENT_CHARS = 32_000
MAX_PDF_PAGES = 50
MAX_SHEET_ROWS = 2_000
MAX_SHEET_COLUMNS = 128
# OOXML files are zip containers; a small upload must not expand without bound.
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSX_MACRO_MEDIA = "application/vnd.ms-excel.sheet.macroEnabled.12"
_PPTX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_LEGACY_OFFICE_SUFFIXES = frozenset({".doc", ".ppt", ".xls"})
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".html",
        ".json",
        ".log",
        ".markdown",
        ".md",
        ".py",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    }
)


class AttachmentRef(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def media_type(self) -> str: ...

    @property
    def size(self) -> int: ...


class ReadAttachmentTool:
    """Read one file that the host bound to this Run.

    The tool never accepts a path, only a name from its closed set, so a model
    cannot reach the workspace, the upload staging area, or another Run.
    """

    name = "read_attachment"
    description = (
        "Read a text, PDF, Word, Excel, PowerPoint or HTML file attached to "
        "this conversation turn."
    )

    def __init__(self, root: Path, attachments: tuple[AttachmentRef, ...]) -> None:
        self.root = root.resolve()
        self.attachments = {attachment.name: attachment for attachment in attachments}

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": sorted(self.attachments),
                        "description": "Exact attachment name from the turn's manifest.",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(arguments, {"name"}, tool_label="attachment tool")
        name = arguments.get("name")
        if not isinstance(name, str) or name not in self.attachments:
            raise InvalidArguments("Attachment name is not part of this turn")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        del context
        self.validate(arguments)
        attachment = self.attachments[str(arguments["name"])]
        path = (self.root / attachment.name).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError("Attachment is no longer available")
        text = await asyncio.to_thread(_extract, path, attachment)
        if len(text) > MAX_ATTACHMENT_CHARS:
            text = (
                text[:MAX_ATTACHMENT_CHARS]
                + f"\n[attachment truncated after {MAX_ATTACHMENT_CHARS} characters]"
            )
        return ToolResult(stdout=text, exit_code=0)


def _extract(path: Path, attachment: AttachmentRef) -> str:
    media = attachment.media_type
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if media == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(data, attachment)
    if media == _DOCX_MEDIA or suffix == ".docx":
        return _office_text(_docx_text, data, attachment, "Word document")
    if media in {_XLSX_MEDIA, _XLSX_MACRO_MEDIA} or suffix in {".xlsx", ".xlsm"}:
        return _office_text(_xlsx_text, data, attachment, "Excel workbook")
    if media == _PPTX_MEDIA or suffix == ".pptx":
        return _office_text(_pptx_text, data, attachment, "PowerPoint deck")
    if media == "text/html" or suffix in {".html", ".htm"}:
        return _safe(_html_text, data, attachment, "HTML file")
    if suffix in _LEGACY_OFFICE_SUFFIXES:
        return (
            f"Legacy Office attachment {attachment.name} ({media}, "
            f"{attachment.size} bytes). Save it as .docx, .xlsx or .pptx to "
            "make it readable."
        )
    if (
        media.startswith("text/")
        or media in _TEXT_MEDIA_TYPES
        or suffix in _TEXT_SUFFIXES
    ):
        return data.decode("utf-8", errors="replace")
    if media.startswith("image/"):
        return (
            f"Image attachment {attachment.name} ({media}, {attachment.size} bytes). "
            "This terminal stores the image but cannot read its pixels."
        )
    return (
        f"Binary attachment {attachment.name} ({media}, {attachment.size} bytes). "
        "Only text, PDF, Office and HTML attachments can be read."
    )


def _office_text(
    parser: Callable[[bytes], str],
    data: bytes,
    attachment: AttachmentRef,
    label: str,
) -> str:
    if _expands_too_far(data):
        return (
            f"{label} attachment {attachment.name} expands beyond the "
            "reading limit; save a smaller copy to read it."
        )
    return _safe(parser, data, attachment, label)


def _expands_too_far(data: bytes) -> bool:
    try:
        with ZipFile(BytesIO(data)) as archive:
            return (
                sum(item.file_size for item in archive.infolist())
                > MAX_UNCOMPRESSED_BYTES
            )
    except Exception:
        # A malformed container is reported by the parser itself.
        return False


def _safe(
    parser: Callable[[bytes], str],
    data: bytes,
    attachment: AttachmentRef,
    label: str,
) -> str:
    try:
        return parser(data)
    except Exception:
        return f"{label} attachment {attachment.name} could not be parsed as text."


def _docx_text(data: bytes) -> str:
    document = Document(BytesIO(data))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _xlsx_text(data: bytes) -> str:
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        characters = 0
        rows = 0
        for sheet in workbook.worksheets:
            parts.append(f"[Sheet: {sheet.title}]")
            values_iter = sheet.iter_rows(
                max_col=min(sheet.max_column or MAX_SHEET_COLUMNS, MAX_SHEET_COLUMNS),
                values_only=True,
            )
            if sheet.max_column is None or sheet.max_column > MAX_SHEET_COLUMNS:
                parts.append(f"[only the first {MAX_SHEET_COLUMNS} columns were read]")
            for values in islice(values_iter, MAX_SHEET_ROWS - rows + 1):
                if rows == MAX_SHEET_ROWS:
                    parts.append("[workbook truncated]")
                    return "\n".join(parts)
                rows += 1
                cells = ["" if value is None else str(value) for value in values]
                line = "\t".join(cells).rstrip("\t")
                if not line:
                    continue
                parts.append(line)
                characters += len(line) + 1
                if characters > MAX_ATTACHMENT_CHARS:
                    parts.append("[workbook truncated]")
                    return "\n".join(parts)
        return "\n".join(parts)
    finally:
        workbook.close()


def _pptx_text(data: bytes) -> str:
    presentation = Presentation(BytesIO(data))
    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, 1):
        parts.append(f"[Slide {index}]")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        parts.append("\t".join(cells))
    return "\n".join(parts)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(data.strip())


def _html_text(data: bytes) -> str:
    parser = _HTMLText()
    parser.feed(data.decode("utf-8", errors="replace"))
    parser.close()
    return "\n".join(parser.parts)


def _extract_pdf(data: bytes, attachment: AttachmentRef) -> str:
    try:
        reader = PdfReader(BytesIO(data))
        pages = reader.pages
        text = "\n".join((page.extract_text() or "") for page in pages[:MAX_PDF_PAGES])
    except Exception:
        return f"PDF attachment {attachment.name} could not be parsed as text."
    if len(pages) > MAX_PDF_PAGES:
        text += f"\n[only the first {MAX_PDF_PAGES} of {len(pages)} pages were read]"
    if not text.strip():
        return (
            f"PDF attachment {attachment.name} has no extractable text; "
            "it may contain only scanned images."
        )
    return text
