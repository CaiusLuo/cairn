"""Coverage for the content-search and file-authoring tools.

These tools are registered into every assembled agent, so their argument
contract, workspace containment and failure behaviour are covered here rather
than only through an end-to-end agent run.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.grep import DEFAULT_LIMIT, GrepTool
from cairn.infra.tools.patch import MAX_HUNKS, PatchTool
from cairn.infra.tools.write import WRITE_FILE_MAX_BYTES, WriteTool
from cairn.infra.workspace.workspace import Workspace


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _grep(root: Path) -> GrepTool:
    return GrepTool(Workspace(root))


def _write_tool(root: Path) -> WriteTool:
    return WriteTool(Workspace(root))


def _patch_tool(root: Path) -> PatchTool:
    return PatchTool(Workspace(root))


# --- grep -----------------------------------------------------------------


def test_grep_reports_path_line_and_text(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "import os\nSECRET = 1\n")

    result = asyncio.run(_grep(tmp_path).execute({"pattern": "SECRET"}))

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["a.py:2: SECRET = 1"]


def test_grep_omitted_limit_uses_the_declared_default(tmp_path: Path) -> None:
    "The schema advertises a default, so omitting limit must not fail."
    for index in range(DEFAULT_LIMIT + 5):
        _write(tmp_path / f"f{index}.py", "needle\n")

    result = asyncio.run(_grep(tmp_path).execute({"pattern": "needle"}))

    assert result.exit_code == 0
    # The limit bounds reported matches; a truncation notice is appended.
    assert len(result.stdout.splitlines()) == DEFAULT_LIMIT + 1
    assert (
        result.stdout.splitlines()[-1] == "[grep output truncated; match limit reached]"
    )
    assert result.stdout_truncated is True


def test_grep_include_filters_candidate_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "needle\n")
    _write(tmp_path / "a.txt", "needle\n")

    result = asyncio.run(
        _grep(tmp_path).execute({"pattern": "needle", "include": "*.py"})
    )

    assert result.stdout.splitlines() == ["a.py:1: needle"]


def test_grep_case_sensitivity_and_literal_mode(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "Needle\nneedle\n")

    sensitive = asyncio.run(_grep(tmp_path).execute({"pattern": "Needle"}))
    assert len(sensitive.stdout.splitlines()) == 1

    insensitive = asyncio.run(
        _grep(tmp_path).execute({"pattern": "Needle", "case_sensitive": False})
    )
    assert len(insensitive.stdout.splitlines()) == 2

    # A regex metacharacter must be inert in literal mode.
    literal = asyncio.run(
        _grep(tmp_path).execute({"pattern": "n..dle", "literal": True})
    )
    assert literal.stdout.startswith("No matches")


def test_grep_reports_no_match_without_failing(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "content\n")

    result = asyncio.run(_grep(tmp_path).execute({"pattern": "absent"}))

    assert result.exit_code == 0
    assert "No matches" in result.stdout


def test_grep_restricts_search_to_a_subpath(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "a.py", "needle\n")
    _write(tmp_path / "other" / "b.py", "needle\n")

    result = asyncio.run(_grep(tmp_path).execute({"pattern": "needle", "path": "src"}))

    assert result.stdout.splitlines() == ["src/a.py:1: needle"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"pattern": ""},
        {"pattern": "   "},
        {"pattern": "ok", "limit": 0},
        {"pattern": "ok", "limit": "3"},
        {"pattern": "ok", "context_lines": -1},
        {"pattern": "ok", "literal": "yes"},
    ],
)
def test_grep_rejects_invalid_arguments(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArguments):
        _grep(tmp_path).validate(arguments)


def test_grep_rejects_an_uncompilable_pattern(tmp_path: Path) -> None:
    with pytest.raises(InvalidArguments):
        _grep(tmp_path).validate({"pattern": "("})


def test_grep_skips_the_git_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".git" / "config", "needle\n")
    _write(tmp_path / "a.py", "needle\n")

    result = asyncio.run(_grep(tmp_path).execute({"pattern": "needle"}))

    assert result.stdout.splitlines() == ["a.py:1: needle"]


# --- write_file -----------------------------------------------------------


def test_write_creates_a_new_file_and_its_directories(tmp_path: Path) -> None:
    result = asyncio.run(
        _write_tool(tmp_path).execute({"path": "deep/new.txt", "content": "abc\n"})
    )

    assert result.exit_code == 0
    assert result.stdout == "Created deep/new.txt"
    assert (tmp_path / "deep" / "new.txt").read_text(encoding="utf-8") == "abc\n"


def test_write_replaces_existing_content_entirely(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "old contents\n")

    result = asyncio.run(
        _write_tool(tmp_path).execute({"path": "a.txt", "content": "new"})
    )

    assert result.stdout == "Replaced a.txt"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new"


def test_write_preserves_the_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "a.sh"
    _write(path, "old\n")
    path.chmod(0o755)

    asyncio.run(_write_tool(tmp_path).execute({"path": "a.sh", "content": "new\n"}))

    assert path.stat().st_mode & 0o777 == 0o755


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    asyncio.run(_write_tool(tmp_path).execute({"path": "a.txt", "content": "x"}))

    assert [item.name for item in tmp_path.iterdir()] == ["a.txt"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "a.txt"},
        {"path": "a.txt", "content": 1},
    ],
)
def test_write_rejects_invalid_arguments(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArguments):
        _write_tool(tmp_path).validate(arguments)


def test_write_rejects_content_over_the_byte_limit(tmp_path: Path) -> None:
    oversized = "x" * (WRITE_FILE_MAX_BYTES + 1)

    with pytest.raises(InvalidArguments):
        _write_tool(tmp_path).validate({"path": "a.txt", "content": oversized})


# --- patch_file -----------------------------------------------------------


def test_patch_applies_multiple_hunks_in_order(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "one\ntwo\nthree\n")

    result = asyncio.run(
        _patch_tool(tmp_path).execute(
            {
                "path": "a.txt",
                "patches": [
                    {"old_text": "one", "new_text": "1"},
                    {"old_text": "three", "new_text": "3"},
                ],
            }
        )
    )

    assert result.exit_code == 0
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "1\ntwo\n3\n"


def test_patch_later_hunk_sees_the_earlier_result(tmp_path: Path) -> None:
    "Hunks apply sequentially, so a later hunk may match earlier output."
    _write(tmp_path / "a.txt", "alpha\n")

    asyncio.run(
        _patch_tool(tmp_path).execute(
            {
                "path": "a.txt",
                "patches": [
                    {"old_text": "alpha", "new_text": "beta"},
                    {"old_text": "beta", "new_text": "gamma"},
                ],
            }
        )
    )

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "gamma\n"


def test_patch_requires_a_unique_match(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "dup\ndup\n")

    with pytest.raises(ValueError):
        asyncio.run(
            _patch_tool(tmp_path).execute(
                {"path": "a.txt", "patches": [{"old_text": "dup", "new_text": "x"}]}
            )
        )
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "dup\ndup\n"


def test_patch_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            _patch_tool(tmp_path).execute(
                {"path": "absent.txt", "patches": [{"old_text": "a", "new_text": "b"}]}
            )
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "a.txt"},
        {"path": "a.txt", "patches": []},
        {"path": "a.txt", "patches": "not-a-list"},
        {"path": "a.txt", "patches": [{"old_text": "", "new_text": "x"}]},
        {"path": "a.txt", "patches": [{"old_text": "a"}]},
        {"path": "a.txt", "patches": [{"old_text": "a", "new_text": "b", "c": 1}]},
        {"path": "a.txt", "patches": [{"old_text": "a", "new_text": 1}]},
    ],
)
def test_patch_rejects_invalid_arguments(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArguments):
        _patch_tool(tmp_path).validate(arguments)


def test_patch_rejects_too_many_hunks(tmp_path: Path) -> None:
    patches = [
        {"old_text": f"a{index}", "new_text": "b"} for index in range(MAX_HUNKS + 1)
    ]

    with pytest.raises(InvalidArguments):
        _patch_tool(tmp_path).validate({"path": "a.txt", "patches": patches})
