from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TextIO

import pytest

from cairn.evals import checks as checks_module
from cairn.evals.checks import (
    FileContainsCheck,
    FileContentEqualsCheck,
    FileExistsCheck,
    FileNotContainsCheck,
)
from cairn.evals.models import EvalCheck
from cairn.workspace.workspace import Workspace


def test_file_exists_check_passes_and_fails_for_missing_or_non_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "answer.txt").write_text("hello", encoding="utf-8")
    workspace = Workspace(tmp_path)

    passed = asyncio.run(FileExistsCheck("answer.txt").evaluate(workspace))
    missing = asyncio.run(FileExistsCheck("missing.txt").evaluate(workspace))
    directory = asyncio.run(FileExistsCheck(".").evaluate(workspace))

    assert passed.passed and passed.message is None
    assert not missing.passed and "missing.txt" in (missing.message or "")
    assert not directory.passed and "regular file" in (directory.message or "")


@pytest.mark.parametrize(
    "check",
    [
        FileContentEqualsCheck("missing.txt", "expected"),
        FileContainsCheck("missing.txt", "needle"),
        FileNotContainsCheck("missing.txt", "needle"),
    ],
)
def test_content_checks_fail_for_missing_file(tmp_path: Path, check: EvalCheck) -> None:
    result = asyncio.run(check.evaluate(Workspace(tmp_path)))

    assert result.passed is False
    assert result.error is None
    assert "missing.txt" in (result.message or "")


@pytest.mark.parametrize("raw_path", ["../outside", "/tmp/outside", ".git/config"])
def test_checks_reject_unsafe_paths(tmp_path: Path, raw_path: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(FileExistsCheck(raw_path).evaluate(Workspace(tmp_path)))


def test_checks_reject_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        asyncio.run(FileExistsCheck("alias.txt").evaluate(Workspace(tmp_path)))


@pytest.mark.parametrize(
    "check",
    [
        FileContentEqualsCheck("missing.txt", 1),  # type: ignore[arg-type]
        FileContainsCheck("missing.txt", None),  # type: ignore[arg-type]
        FileNotContainsCheck("missing.txt", False),  # type: ignore[arg-type]
    ],
)
def test_content_checks_reject_malformed_expectations(
    tmp_path: Path, check: EvalCheck
) -> None:
    with pytest.raises(ValueError, match="string"):
        asyncio.run(check.evaluate(Workspace(tmp_path)))


def test_unexpected_read_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "answer.txt").write_text("hello", encoding="utf-8")

    def fail_open(_path: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("read denied")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(PermissionError, match="read denied"):
        asyncio.run(
            FileContainsCheck("answer.txt", "hello").evaluate(Workspace(tmp_path))
        )


class _ReadSpy:
    def __init__(
        self,
        stream: TextIO,
        read_sizes: list[int],
        closed_states: list[bool],
    ) -> None:
        self.stream = stream
        self.read_sizes = read_sizes
        self.closed_states = closed_states

    def __enter__(self) -> _ReadSpy:
        return self

    def __exit__(self, *args: Any) -> None:
        self.stream.close()
        self.closed_states.append(self.stream.closed)

    def read(self, size: int = -1) -> str:
        self.read_sizes.append(size)
        return self.stream.read(size)


def _spy_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], list[bool]]:
    read_sizes: list[int] = []
    closed_states: list[bool] = []
    original_open = Path.open

    def tracking_open(path: Path, *args: Any, **kwargs: Any) -> _ReadSpy:
        stream = original_open(path, *args, **kwargs)
        return _ReadSpy(stream, read_sizes, closed_states)

    def forbidden_read_text(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("content checks must not use Path.read_text")

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    return read_sizes, closed_states


@pytest.mark.parametrize(
    ("contents", "check", "passed", "expected_reads"),
    [
        pytest.param(
            "Xbcdefghijk",
            FileContentEqualsCheck("answer.txt", "abcdefghijk"),
            False,
            [4],
            id="equals-first-mismatch",
        ),
        pytest.param(
            "abcdefghijk",
            FileContentEqualsCheck("answer.txt", "abcdefghij"),
            False,
            [4, 4, 4],
            id="equals-trailing-content",
        ),
        pytest.param(
            "abcdefgh",
            FileContentEqualsCheck("answer.txt", "abcdefghijk"),
            False,
            [4, 4, 4],
            id="equals-short-eof",
        ),
        pytest.param(
            "abcdefghijkl",
            FileContentEqualsCheck("answer.txt", "abcdefghijkl"),
            True,
            [4, 4, 4, 4],
            id="equals-full-multi-chunk-match",
        ),
        pytest.param(
            "abcdefgh",
            FileContainsCheck("answer.txt", "missing"),
            False,
            [4, 4, 4],
            id="contains-missing-scans-to-eof",
        ),
        pytest.param(
            "abcdefgh",
            FileNotContainsCheck("answer.txt", "missing"),
            True,
            [4, 4, 4],
            id="not-contains-missing-scans-to-eof",
        ),
        pytest.param(
            "needle" + "x" * 100,
            FileContainsCheck("answer.txt", "needle"),
            True,
            [4, 4],
            id="contains-found-early",
        ),
        pytest.param(
            "needle" + "x" * 100,
            FileNotContainsCheck("answer.txt", "needle"),
            False,
            [4, 4],
            id="not-contains-found-early",
        ),
    ],
)
def test_content_checks_use_bounded_reads_and_close_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    check: EvalCheck,
    passed: bool,
    expected_reads: list[int],
) -> None:
    (tmp_path / "answer.txt").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(checks_module, "CHECK_READ_CHUNK_SIZE", 4)
    read_sizes, closed_states = _spy_reads(monkeypatch)

    result = asyncio.run(check.evaluate(Workspace(tmp_path)))

    assert result.passed is passed
    assert read_sizes == expected_reads
    assert all(size == 4 for size in read_sizes)
    assert closed_states == [True]


@pytest.mark.parametrize(
    ("contents", "needle", "contains"),
    [
        ("xxabcdyy", "abcd", True),
        ("xxabcdefghijyy", "abcdefghij", True),
        ("xxabcyy", "abcd", False),
    ],
)
@pytest.mark.parametrize("check_type", [FileContainsCheck, FileNotContainsCheck])
def test_substring_checks_match_across_chunk_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    needle: str,
    contains: bool,
    check_type: type[FileContainsCheck] | type[FileNotContainsCheck],
) -> None:
    (tmp_path / "answer.txt").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(checks_module, "CHECK_READ_CHUNK_SIZE", 4)
    result = asyncio.run(check_type("answer.txt", needle).evaluate(Workspace(tmp_path)))

    assert result.passed is (
        contains if check_type is FileContainsCheck else not contains
    )


@pytest.mark.parametrize("contents", ["", "readable text"])
@pytest.mark.parametrize(
    ("check_type", "expected_pass"),
    [(FileContainsCheck, True), (FileNotContainsCheck, False)],
)
def test_substring_checks_treat_empty_needle_consistently(
    tmp_path: Path,
    contents: str,
    check_type: type[FileContainsCheck] | type[FileNotContainsCheck],
    expected_pass: bool,
) -> None:
    (tmp_path / "answer.txt").write_text(contents, encoding="utf-8")
    result = asyncio.run(check_type("answer.txt", "").evaluate(Workspace(tmp_path)))

    assert result.passed is expected_pass


@pytest.mark.parametrize(
    ("check", "expected_pass"),
    [
        (FileContentEqualsCheck("answer.txt", "café\nready\n"), True),
        (FileContainsCheck("answer.txt", "é\nre"), True),
        (FileNotContainsCheck("answer.txt", "é\r\n"), True),
    ],
)
def test_content_checks_preserve_utf8_and_crlf_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: EvalCheck,
    expected_pass: bool,
) -> None:
    (tmp_path / "answer.txt").write_bytes("café\r\nready\r\n".encode())
    monkeypatch.setattr(checks_module, "CHECK_READ_CHUNK_SIZE", 4)

    result = asyncio.run(check.evaluate(Workspace(tmp_path)))

    assert result.passed is expected_pass


@pytest.mark.parametrize(
    "check",
    [
        FileContentEqualsCheck("answer.txt", "expected"),
        FileContainsCheck("answer.txt", "needle"),
        FileNotContainsCheck("answer.txt", "needle"),
    ],
)
def test_content_checks_propagate_invalid_utf8(
    tmp_path: Path, check: EvalCheck
) -> None:
    (tmp_path / "answer.txt").write_bytes(b"valid prefix\xffinvalid")

    with pytest.raises(UnicodeDecodeError):
        asyncio.run(check.evaluate(Workspace(tmp_path)))


def test_failure_messages_use_the_logical_path_exactly(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "answer.txt").write_text("hello", encoding="utf-8")
    workspace = Workspace(tmp_path)
    checks_and_messages: list[tuple[EvalCheck, str]] = [
        (
            FileExistsCheck("docs/missing.txt"),
            "Expected a regular file at docs/missing.txt",
        ),
        (
            FileContentEqualsCheck("docs/answer.txt", "expected"),
            "Expected docs/answer.txt to equal 'expected'",
        ),
        (
            FileContainsCheck("docs/answer.txt", "needle"),
            "Expected docs/answer.txt to contain 'needle'",
        ),
        (
            FileNotContainsCheck("docs/answer.txt", "hello"),
            "Expected docs/answer.txt not to contain 'hello'",
        ),
    ]

    for check, expected_message in checks_and_messages:
        result = asyncio.run(check.evaluate(workspace))
        assert result.passed is False
        assert result.message == expected_message
