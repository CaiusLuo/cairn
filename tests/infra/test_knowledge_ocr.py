"""PDF OCR fallback: page selection, work limits, failures and cleanup."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cairn.knowledge.ocr import OCR_PARSER, OcrSettings, PdfOcrExtractor
from cairn.shared.text_extract import (
    ExtractionUnavailable,
    PdfExtraction,
    PdfPageText,
)


def _pdf(*texts: str) -> PdfExtraction:
    return PdfExtraction(
        pages=tuple(PdfPageText(index, text) for index, text in enumerate(texts)),
        total_pages=len(texts),
    )


class FakeRecognizer:
    def __init__(self, results: dict[int, str | Exception]) -> None:
        self.results = results
        self.calls: list[int] = []
        self.closed = False

    def recognize(self, page_index: int) -> str:
        self.calls.append(page_index)
        result = self.results[page_index]
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def _extractor(
    pdf: PdfExtraction,
    factory: Callable[[bytes, int], FakeRecognizer],
    *,
    settings: OcrSettings | None = None,
    clock: Callable[[], float] | None = None,
) -> PdfOcrExtractor:
    return PdfOcrExtractor(
        factory,
        settings,
        pages_loader=lambda _name, _data, _max_pages: pdf,
        clock=clock or (lambda: 0.0),
    )


def test_text_layer_pages_never_load_the_backend() -> None:
    pdf = _pdf("This page already has a full text layer." * 2)

    def factory(_data: bytes, _dpi: int) -> FakeRecognizer:
        raise AssertionError("OCR backend must not load")

    result = _extractor(pdf, factory).extract("scan.pdf", b"%PDF", max_pdf_pages=10)

    assert result.parser == "pdf"
    assert "full text layer" in result.text


def test_missing_pages_are_recognized_in_order_and_closed() -> None:
    pdf = _pdf(
        "Existing text layer content.",
        "",
        "Another existing page with a text layer.",
    )
    recognizer = FakeRecognizer({1: "OCR 第二页"})
    result = _extractor(pdf, lambda _data, _dpi: recognizer).extract(
        "scan.pdf", b"%PDF", max_pdf_pages=10
    )

    assert result.parser == OCR_PARSER
    assert recognizer.calls == [1]
    assert recognizer.closed
    assert (
        result.text.index("Existing")
        < result.text.index("OCR 第二页")
        < result.text.index("Another existing")
    )


def test_ocr_page_limit_marks_remaining_pages() -> None:
    pdf = _pdf("", "", "", "")
    recognizer = FakeRecognizer({0: "one", 1: "two"})
    settings = OcrSettings(max_pages=2, dpi=150, max_seconds=100)

    result = _extractor(pdf, lambda _data, _dpi: recognizer, settings=settings).extract(
        "scan.pdf", b"%PDF", max_pdf_pages=10
    )

    assert recognizer.calls == [0, 1]
    assert result.parser == OCR_PARSER
    assert "[OCR skipped pages: 3, 4]" in result.text


def test_ocr_time_budget_skips_remaining_pages() -> None:
    pdf = _pdf("", "", "")
    recognizer = FakeRecognizer({0: "one", 1: "two"})
    times = iter([0.0, 0.0, 200.0, 200.0])

    result = _extractor(
        pdf,
        lambda _data, _dpi: recognizer,
        settings=OcrSettings(max_pages=10, max_seconds=120),
        clock=lambda: next(times),
    ).extract("scan.pdf", b"%PDF", max_pdf_pages=10)

    assert recognizer.calls == [0]
    assert "[OCR skipped pages: 2, 3]" in result.text


def test_failed_page_keeps_other_text_and_closes_backend() -> None:
    pdf = _pdf("", "", "Existing text layer with enough content.")
    recognizer = FakeRecognizer({0: "first page", 1: RuntimeError("boom")})

    result = _extractor(pdf, lambda _data, _dpi: recognizer).extract(
        "scan.pdf", b"%PDF", max_pdf_pages=10
    )

    assert result.parser == OCR_PARSER
    assert "first page" in result.text
    assert "Existing text layer" in result.text
    assert "[OCR skipped pages: 2]" in result.text
    assert recognizer.closed


def test_unavailable_backend_keeps_text_layer() -> None:
    pdf = _pdf("Existing text layer with enough content.", "")

    def factory(_data: bytes, _dpi: int) -> FakeRecognizer:
        raise ImportError("OCR extra is not installed")

    result = _extractor(pdf, factory).extract("scan.pdf", b"%PDF", max_pdf_pages=10)

    assert result.parser == "pdf"
    assert "Existing text layer" in result.text
    assert "[OCR skipped pages: 2]" in result.text


def test_unavailable_backend_fails_when_no_text_was_read() -> None:
    pdf = _pdf("", "")

    def factory(_data: bytes, _dpi: int) -> FakeRecognizer:
        raise ImportError("OCR extra is not installed")

    with pytest.raises(ExtractionUnavailable, match="OCR could not read"):
        _extractor(pdf, factory).extract("scan.pdf", b"%PDF", max_pdf_pages=10)


def test_zero_page_limit_does_not_load_the_backend() -> None:
    pdf = _pdf("", "")

    def factory(_data: bytes, _dpi: int) -> FakeRecognizer:
        raise AssertionError("OCR backend must not load")

    with pytest.raises(ExtractionUnavailable, match="OCR could not read"):
        _extractor(pdf, factory, settings=OcrSettings(max_pages=0)).extract(
            "scan.pdf", b"%PDF", max_pdf_pages=10
        )


def test_pdf_without_pages_is_rejected() -> None:
    pdf = PdfExtraction(pages=(), total_pages=0)

    with pytest.raises(ExtractionUnavailable, match="no pages"):
        _extractor(pdf, lambda _data, _dpi: FakeRecognizer({})).extract(
            "scan.pdf", b"%PDF", max_pdf_pages=10
        )
