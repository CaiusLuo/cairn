"""Plain-text extraction for uploaded files.

Both the per-Run attachment tool and the persistent document library read the
same file formats through this module. Parsers are pure: callers own storage,
limits on how much text they keep, and what a failure means for them.

`extract_bytes` raises `ExtractionUnavailable` when the bytes cannot be read as
text; its message is already safe to show to a person and names no path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from itertools import islice
from zipfile import ZipFile

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

# Bump when parser output changes shape enough to need re-extraction.
PARSER_VERSION = "1"
MAX_CHARS = 32_000
MAX_PDF_PAGES = 50
MAX_SHEET_ROWS = 2_000
MAX_SHEET_COLUMNS = 128
# OOXML files are zip containers; a small upload must not expand without bound.
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_MACRO_MEDIA = "application/vnd.ms-excel.sheet.macroEnabled.12"
PPTX_MEDIA = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
LEGACY_OFFICE_SUFFIXES = frozenset({".doc", ".ppt", ".xls"})
TEXT_SUFFIXES = frozenset(
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
TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    }
)
SUPPORTED_SUFFIXES = frozenset(
    {
        ".csv",
        ".docx",
        ".htm",
        ".html",
        ".json",
        ".log",
        ".markdown",
        ".md",
        ".pdf",
        ".pptx",
        ".py",
        ".toml",
        ".tsv",
        ".txt",
        ".xlsm",
        ".xlsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class ExtractionUnavailable(ValueError):
    """These bytes cannot be read as plain text by a supported parser."""


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    parser: str
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True, slots=True)
class PdfPageText:
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    pages: tuple[PdfPageText, ...]
    total_pages: int

    def joined(self, max_pdf_pages: int) -> str:
        text = "\n".join(page.text for page in self.pages)
        if self.total_pages > max_pdf_pages:
            text += (
                f"\n[only the first {max_pdf_pages} of {self.total_pages} "
                "pages were read]"
            )
        return text


def extract_bytes(
    name: str,
    media_type: str,
    size: int,
    data: bytes,
    *,
    max_chars: int = MAX_CHARS,
    max_pdf_pages: int = MAX_PDF_PAGES,
) -> ExtractedText:
    """Extract bounded text from one stored file.

    ``max_chars`` bounds only the streaming spreadsheet parser; every caller
    still decides how much of the returned text to keep. ``max_pdf_pages``
    bounds PDF extraction and is reported inside the text when hit.
    """
    media = (media_type or "application/octet-stream").split(";")[0].strip().lower()
    suffix = _suffix(name)
    if is_pdf(name, media):
        extraction = extract_pdf_pages(name, data, max_pdf_pages)
        text = extraction.joined(max_pdf_pages)
        if not text.strip():
            raise ExtractionUnavailable(
                f"PDF attachment {name} has no extractable text; "
                "it may contain only scanned images."
            )
        return ExtractedText(text, "pdf")
    if media == DOCX_MEDIA or suffix == ".docx":
        return ExtractedText(
            _office_text(_docx_text, data, name, "Word document"), "docx"
        )
    if media in {XLSX_MEDIA, XLSX_MACRO_MEDIA} or suffix in {".xlsx", ".xlsm"}:
        return ExtractedText(
            _office_text(
                lambda payload: _xlsx_text(payload, max_chars),
                data,
                name,
                "Excel workbook",
            ),
            "xlsx",
        )
    if media == PPTX_MEDIA or suffix == ".pptx":
        return ExtractedText(
            _office_text(_pptx_text, data, name, "PowerPoint deck"), "pptx"
        )
    if media == "text/html" or suffix in {".html", ".htm"}:
        return ExtractedText(_safe(_html_text, data, name, "HTML file"), "html")
    if suffix in LEGACY_OFFICE_SUFFIXES:
        raise ExtractionUnavailable(
            f"Legacy Office attachment {name} ({media_type}, {size} bytes). "
            "Save it as .docx, .xlsx or .pptx to make it readable."
        )
    if (
        media.startswith("text/")
        or media in TEXT_MEDIA_TYPES
        or suffix in TEXT_SUFFIXES
    ):
        return ExtractedText(data.decode("utf-8", errors="replace"), "text")
    if media.startswith("image/"):
        raise ExtractionUnavailable(
            f"Image attachment {name} ({media_type}, {size} bytes). "
            "This terminal stores the image but cannot read its pixels."
        )
    raise ExtractionUnavailable(
        f"Binary attachment {name} ({media_type}, {size} bytes). "
        "Only text, PDF, Office and HTML attachments can be read."
    )


def is_pdf(name: str, media_type: str) -> bool:
    media = (media_type or "").split(";")[0].strip().lower()
    return media == "application/pdf" or _suffix(name) == ".pdf"


def is_supported(name: str, media_type: str) -> bool:
    """Whether `extract_bytes` can produce text for this file at all."""
    media = (media_type or "").split(";")[0].strip().lower()
    suffix = _suffix(name)
    return (
        is_pdf(name, media)
        or media in {DOCX_MEDIA, XLSX_MEDIA, XLSX_MACRO_MEDIA, PPTX_MEDIA, "text/html"}
        or media.startswith("text/")
        or media in TEXT_MEDIA_TYPES
        or suffix in SUPPORTED_SUFFIXES
    )


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _office_text(
    parser: Callable[[bytes], str], data: bytes, name: str, label: str
) -> str:
    if _expands_too_far(data):
        raise ExtractionUnavailable(
            f"{label} attachment {name} expands beyond the reading limit; "
            "save a smaller copy to read it."
        )
    return _safe(parser, data, name, label)


def _safe(parser: Callable[[bytes], str], data: bytes, name: str, label: str) -> str:
    try:
        return parser(data)
    except Exception as error:
        raise ExtractionUnavailable(
            f"{label} attachment {name} could not be parsed as text."
        ) from error


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


def _xlsx_text(data: bytes, max_chars: int) -> str:
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
                if characters > max_chars:
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


def extract_pdf_pages(
    name: str, data: bytes, max_pdf_pages: int = MAX_PDF_PAGES
) -> PdfExtraction:
    """Read the text layer of each page; OCR belongs to the caller."""
    try:
        reader = PdfReader(BytesIO(data))
        pages = reader.pages
        texts = tuple(
            PdfPageText(index, page.extract_text() or "")
            for index, page in enumerate(pages[:max_pdf_pages])
        )
    except Exception as error:
        raise ExtractionUnavailable(
            f"PDF attachment {name} could not be parsed as text."
        ) from error
    return PdfExtraction(texts, len(pages))
