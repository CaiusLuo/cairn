"""OCR fallback for PDF pages without a usable text layer.

Only pages whose text layer is missing or tiny are rendered and recognized.
The backend is imported lazily, so a text-layer PDF never loads the models and
the service can start without the optional OCR dependencies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from cairn.shared.text_extract import (
    ExtractedText,
    ExtractionUnavailable,
    PdfExtraction,
    PdfPageText,
    extract_pdf_pages,
)

LOGGER = logging.getLogger(__name__)
MIN_PAGE_CHARS = 20
OCR_PARSER = "pdf+ocr"


class PageRecognizer(Protocol):
    def recognize(self, page_index: int) -> str: ...

    def close(self) -> None: ...


type RecognizerFactory = Callable[[bytes, int], PageRecognizer]
type PageLoader = Callable[[str, bytes, int], PdfExtraction]


@dataclass(frozen=True, slots=True)
class OcrSettings:
    max_pages: int = 30
    dpi: int = 200
    max_seconds: float = 120.0


class PdfOcr(Protocol):
    def extract(
        self, name: str, data: bytes, *, max_pdf_pages: int
    ) -> ExtractedText: ...


class PdfOcrExtractor:
    def __init__(
        self,
        factory: RecognizerFactory,
        settings: OcrSettings | None = None,
        *,
        pages_loader: PageLoader = extract_pdf_pages,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.factory = factory
        self.settings = settings or OcrSettings()
        self.pages_loader = pages_loader
        self.clock = clock

    def extract(self, name: str, data: bytes, *, max_pdf_pages: int) -> ExtractedText:
        pdf = self.pages_loader(name, data, max_pdf_pages)
        if not pdf.pages:
            raise ExtractionUnavailable(
                f"PDF attachment {name} has no extractable text; it has no pages."
            )
        texts = [page.text for page in pdf.pages]
        missing = [page for page in pdf.pages if _needs_ocr(page.text)]
        if not missing:
            return ExtractedText(pdf.joined(max_pdf_pages), "pdf")

        attempted = 0
        recognized_pages = 0
        skipped: list[int] = []
        recognizer: PageRecognizer | None = None
        if self.settings.max_pages <= 0 or self.settings.max_seconds <= 0:
            skipped.extend(page.index for page in missing)
        else:
            started = self.clock()
            try:
                recognizer = self.factory(data, self.settings.dpi)
            except MemoryError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "OCR backend unavailable for %s: %s", name, type(error).__name__
                )
                skipped.extend(page.index for page in missing)
            else:
                for page in missing:
                    if (
                        attempted >= self.settings.max_pages
                        or self.clock() - started >= self.settings.max_seconds
                    ):
                        skipped.append(page.index)
                        continue
                    attempted += 1
                    try:
                        recognized = recognizer.recognize(page.index)
                    except MemoryError:
                        raise
                    except Exception as error:
                        LOGGER.warning(
                            "OCR page %d failed for %s: %s",
                            page.index + 1,
                            name,
                            type(error).__name__,
                        )
                        skipped.append(page.index)
                        continue
                    if recognized.strip():
                        texts[page.index] = recognized.strip()
                        recognized_pages += 1
                    else:
                        skipped.append(page.index)
            finally:
                if recognizer is not None:
                    with suppress(Exception):
                        recognizer.close()

        if not any(text.strip() for text in texts):
            raise ExtractionUnavailable(
                f"PDF attachment {name} has no extractable text; "
                "OCR could not read any page."
            )
        updated = PdfExtraction(
            pages=tuple(
                PdfPageText(page.index, texts[page.index]) for page in pdf.pages
            ),
            total_pages=pdf.total_pages,
        )
        text = updated.joined(max_pdf_pages)
        if skipped:
            shown = ", ".join(str(index + 1) for index in skipped[:20])
            if len(skipped) > 20:
                shown += ", ..."
            text += f"\n\n[OCR skipped pages: {shown}]"
        return ExtractedText(text, OCR_PARSER if recognized_pages else "pdf")


def _needs_ocr(text: str) -> bool:
    return sum(1 for char in text if not char.isspace()) < MIN_PAGE_CHARS


def rapidocr_recognizer(data: bytes, dpi: int) -> PageRecognizer:
    """Load the optional RapidOCR backend only when a page needs OCR."""
    from cairn.knowledge.ocr_rapid import RapidOcrRecognizer

    return RapidOcrRecognizer(data, dpi=dpi)


__all__ = [
    "MIN_PAGE_CHARS",
    "OCR_PARSER",
    "OcrSettings",
    "PageRecognizer",
    "PdfOcr",
    "PdfOcrExtractor",
    "rapidocr_recognizer",
]
