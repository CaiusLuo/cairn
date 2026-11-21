"""Deterministic chunking and shared text extraction for the document library."""

from __future__ import annotations

from io import BytesIO
from itertools import pairwise

import pytest
from pypdf import PdfWriter

from cairn.knowledge.documents import (
    chunk_text,
    estimate_tokens,
)
from cairn.shared.text_extract import (
    PARSER_VERSION,
    ExtractionUnavailable,
    extract_bytes,
    extract_pdf_pages,
    is_pdf,
    is_supported,
)


def test_chinese_chunks_cut_on_sentence_boundaries_and_carry_offsets() -> None:
    text = "第一句话讲目标。第二句话讲范围！第三句话讲步骤？第四句话讲风险；" * 120  # noqa: RUF001
    chunks = chunk_text(text, chunk_chars=300, overlap=60)

    assert len(chunks) > 10
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)
    for chunk in chunks:
        assert 0 < len(chunk.text) <= 300
        assert text[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.text[-1] in "。！？；"  # noqa: RUF001
        assert chunk.token_count >= 1
    for previous, current in pairwise(chunks):
        # Overlapping windows leave no gap between neighbouring chunks.
        assert current.char_start < previous.char_end


def test_offsets_are_in_the_original_text_when_edges_are_whitespace() -> None:
    text = "\n\n  " + "内容" * 100 + "\n\n  " + "后续" * 100 + "  \n"
    chunks = chunk_text(text, chunk_chars=120, overlap=0)
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
        assert not chunk.text.startswith(" ")
        assert not chunk.text.endswith(" ")


def test_max_chunks_bounds_ingestion_work() -> None:
    text = "段落。" * 5000
    chunks = chunk_text(text, chunk_chars=100, overlap=10, max_chunks=5)
    assert len(chunks) == 5
    assert chunks[-1].char_end < len(text)


def test_blank_text_produces_no_chunks() -> None:
    assert chunk_text("   \n\n\t ") == ()


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text("text", chunk_chars=32)
    with pytest.raises(ValueError):
        chunk_text("text", chunk_chars=100, overlap=100)
    with pytest.raises(ValueError):
        chunk_text("text", max_chunks=0)


def test_markdown_headings_become_a_heading_path() -> None:
    text = (
        "# 项目说明\n\n"
        + "这是项目的第一段，讲的是目标与范围。" * 8  # noqa: RUF001
        + "\n\n## 部署步骤\n\n"
        + "在生产服务器上运行脚本并检查服务状态。" * 8
        + "\n"
    )
    chunks = chunk_text(text, chunk_chars=120, overlap=20, markdown=True)
    paths = {chunk.heading_path for chunk in chunks}
    assert ("项目说明",) in paths
    assert ("项目说明", "部署步骤") in paths


def test_headings_are_ignored_for_plain_text() -> None:
    text = "# not a heading\n\n" + "正文。" * 100
    chunks = chunk_text(text, chunk_chars=100, overlap=0)
    assert all(chunk.heading_path is None for chunk in chunks)


def test_token_estimate_grows_with_content() -> None:
    assert estimate_tokens("中文内容") >= 4
    assert estimate_tokens("hello world") >= 2
    assert estimate_tokens(" ") == 1


def test_text_extraction_reports_its_parser() -> None:
    result = extract_bytes("notes.txt", "text/plain", 4, b"a\xffb")
    assert result.text == "a�b"
    assert result.parser == "text"
    assert result.parser_version == PARSER_VERSION


def test_html_extraction_drops_script_and_style() -> None:
    data = (
        "<html><style>p{color:red}</style><script>alert('x')</script><p>正文</p></html>"
    ).encode()
    result = extract_bytes("page.html", "text/html", len(data), data)
    assert result.text == "正文"
    assert result.parser == "html"


def test_unsupported_binary_is_reported_not_decoded() -> None:
    with pytest.raises(ExtractionUnavailable, match="Binary attachment"):
        extract_bytes("a.bin", "application/octet-stream", 3, b"\x00\x01\x02")


def test_supported_types_are_recognized_by_suffix_or_media_type() -> None:
    assert is_supported("a.pdf", "application/octet-stream")
    assert is_supported("笔记.md", "application/octet-stream")
    assert is_supported("payload", "text/plain")
    assert not is_supported("scan.png", "image/png")
    assert not is_supported("old.doc", "application/msword")
    assert is_pdf("a.pdf", "application/octet-stream")
    assert is_pdf("payload", "application/pdf; charset=binary")
    assert not is_pdf("a.txt", "text/plain")


def test_pdf_pages_are_exposed_for_the_ocr_fallback() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()

    extraction = extract_pdf_pages("scan.pdf", data, max_pdf_pages=1)
    assert extraction.total_pages == 2
    assert [page.index for page in extraction.pages] == [0]
    assert extraction.pages[0].text.strip() == ""
    with pytest.raises(ExtractionUnavailable, match="no extractable text"):
        extract_bytes("scan.pdf", "application/pdf", len(data), data, max_pdf_pages=2)
