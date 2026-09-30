import asyncio
from pathlib import Path

import pytest

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
    ("check", "expected_pass"),
    [
        (FileContentEqualsCheck("answer.txt", "héllo"), True),
        (FileContentEqualsCheck("answer.txt", "goodbye"), False),
        (FileContainsCheck("answer.txt", "éll"), True),
        (FileContainsCheck("answer.txt", "missing"), False),
        (FileNotContainsCheck("answer.txt", "missing"), True),
        (FileNotContainsCheck("answer.txt", "éll"), False),
    ],
)
def test_content_checks_match_utf8_file(
    tmp_path: Path, check: EvalCheck, expected_pass: bool
) -> None:
    (tmp_path / "answer.txt").write_text("héllo", encoding="utf-8")
    result = asyncio.run(check.evaluate(Workspace(tmp_path)))

    assert result.passed is expected_pass
    assert (result.message is None) is expected_pass


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

    def fail_read(_path: Path, *, encoding: str | None = None) -> str:
        raise PermissionError("read denied")

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(PermissionError, match="read denied"):
        asyncio.run(
            FileContainsCheck("answer.txt", "hello").evaluate(Workspace(tmp_path))
        )
