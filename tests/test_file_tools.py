import asyncio
import io
import os
from pathlib import Path

import pytest

from cairn.tools.files import (
    EDIT_FILE_MAX_BYTES,
    READ_FILE_CHUNK_SIZE,
    EditFileTool,
    ReadFileTool,
    _decode_bounded_utf8,
    _read_bounded_range,
)
from cairn.workspace.workspace import Workspace


class RecordingBytesIO(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.readline_sizes: list[int] = []

    def readline(self, size: int | None = -1) -> bytes:
        if size is None:
            raise AssertionError("readline must use an explicit byte limit")
        self.readline_sizes.append(size)
        return super().readline(size)


def test_read_file_returns_requested_lines_and_caps_output(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    tool = ReadFileTool(Workspace(tmp_path))

    result = asyncio.run(
        tool.execute({"path": "example.py", "start_line": 2, "end_line": 3})
    )

    assert result.stdout == "two\nthree\n"
    assert result.exit_code == 0

    path.write_text("x" * 13_000, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert len(result.stdout) < 13_000
    assert result.stdout.endswith("[read_file output truncated]")
    with pytest.raises(ValueError, match="1 to 200 lines"):
        asyncio.run(tool.execute({"path": "example.py", "end_line": 201}))

    path.write_text("line\n" * 201, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert result.stdout.count("line\n") == 200
    assert result.stdout.endswith("[read_file more lines available]")


def test_read_file_bounds_very_long_single_line(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * 1_000_000)

    result = asyncio.run(
        ReadFileTool(Workspace(tmp_path)).execute({"path": "large.txt"})
    )

    assert len(result.stdout) < 20_000
    assert result.stdout_truncated is True
    assert "[read_file output truncated]" in result.stdout


def test_read_file_utf8_truncation_drops_partial_character() -> None:
    stream = io.BytesIO("你你".encode())

    data, truncated, _ = _read_bounded_range(
        stream,
        start_line=1,
        end_line=1,
        capture_limit=4,
    )

    assert truncated is True
    assert _decode_bounded_utf8(data, truncated=True) == "你"


def test_read_file_preserves_carriage_return_line_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "carriage-returns.txt"
    path.write_bytes(b"one\rtwo\rthree\r")

    result = asyncio.run(
        ReadFileTool(Workspace(tmp_path)).execute(
            {
                "path": "carriage-returns.txt",
                "start_line": 2,
                "end_line": 2,
            }
        )
    )

    assert result.stdout == "two\r\n[read_file more lines available]"


def test_read_file_handles_crlf_across_chunk_boundary(tmp_path: Path) -> None:
    path = tmp_path / "split-crlf.txt"
    path.write_bytes(b"a" * (READ_FILE_CHUNK_SIZE - 1) + b"\r\nTARGET\r\n")

    result = asyncio.run(
        ReadFileTool(Workspace(tmp_path)).execute(
            {
                "path": "split-crlf.txt",
                "start_line": 2,
                "end_line": 2,
            }
        )
    )

    assert result.stdout == "TARGET\r\n"


def test_read_file_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"invalid:\xff\n")

    with pytest.raises(UnicodeDecodeError):
        asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": "invalid.txt"}))


def test_read_file_reads_deep_line_without_buffering_prefix(tmp_path: Path) -> None:
    path = tmp_path / "deep.txt"
    path.write_text("skip\n" * 10_000 + "TARGET\n", encoding="utf-8")
    tool = ReadFileTool(Workspace(tmp_path))

    result = asyncio.run(
        tool.execute(
            {
                "path": "deep.txt",
                "start_line": 10_001,
                "end_line": 10_001,
            }
        )
    )

    assert result.stdout == "TARGET\n"


def test_read_file_never_requests_unbounded_line_read() -> None:
    stream = RecordingBytesIO(b"a" * 1_000_000)

    _read_bounded_range(
        stream,
        start_line=1,
        end_line=1,
    )

    assert stream.readline_sizes
    assert all(size == READ_FILE_CHUNK_SIZE for size in stream.readline_sizes)


def test_edit_file_replaces_once_and_leaves_failed_edits_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "example.py"
    path.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    tool = EditFileTool(Workspace(tmp_path))

    result = asyncio.run(
        tool.execute({"path": "example.py", "old_text": "beta", "new_text": "gamma"})
    )

    assert result.stdout == "Updated example.py"
    assert path.read_text(encoding="utf-8") == "alpha\ngamma\nalpha\n"

    for old_text, count in (("alpha", 2), ("missing", 0)):
        with pytest.raises(ValueError, match=f"matched {count} times"):
            asyncio.run(
                tool.execute(
                    {"path": "example.py", "old_text": old_text, "new_text": "bad"}
                )
            )
        assert path.read_text(encoding="utf-8") == "alpha\ngamma\nalpha\n"


def test_edit_file_rejects_oversized_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    original = b"a" * (EDIT_FILE_MAX_BYTES + 1)
    path.write_bytes(original)
    tool = EditFileTool(Workspace(tmp_path))

    with pytest.raises(ValueError, match="exceeds edit_file limit"):
        asyncio.run(
            tool.execute(
                {
                    "path": "large.txt",
                    "old_text": "a",
                    "new_text": "b",
                }
            )
        )

    assert path.read_bytes() == original


def test_edit_file_rejects_oversized_result(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("hello", encoding="utf-8")
    tool = EditFileTool(Workspace(tmp_path))

    with pytest.raises(ValueError, match="would exceed edit_file limit"):
        asyncio.run(
            tool.execute(
                {
                    "path": "file.txt",
                    "old_text": "hello",
                    "new_text": "x" * (EDIT_FILE_MAX_BYTES + 1),
                }
            )
        )

    assert path.read_text(encoding="utf-8") == "hello"


def test_edit_file_rejects_oversized_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "new.txt"
    tool = EditFileTool(Workspace(tmp_path))

    def unexpected_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("oversized create reached temporary file creation")

    monkeypatch.setattr("cairn.tools.files.tempfile.mkstemp", unexpected_mkstemp)

    with pytest.raises(ValueError, match="would exceed edit_file limit"):
        asyncio.run(
            tool.execute(
                {
                    "path": "new.txt",
                    "old_text": "",
                    "new_text": "x" * (EDIT_FILE_MAX_BYTES + 1),
                }
            )
        )

    assert not path.exists()


def test_edit_file_limit_counts_utf8_bytes(tmp_path: Path) -> None:
    path = tmp_path / "utf8.txt"
    text = "你" * (EDIT_FILE_MAX_BYTES // 3 + 1)
    tool = EditFileTool(Workspace(tmp_path))

    with pytest.raises(ValueError):
        asyncio.run(
            tool.execute(
                {
                    "path": "utf8.txt",
                    "old_text": "",
                    "new_text": text,
                }
            )
        )

    assert not path.exists()


def test_edit_file_keeps_original_if_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "example.py"
    path.write_text("before\n", encoding="utf-8")

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        asyncio.run(
            EditFileTool(Workspace(tmp_path)).execute(
                {"path": "example.py", "old_text": "before", "new_text": "after"}
            )
        )

    assert path.read_text(encoding="utf-8") == "before\n"
    assert not list(tmp_path.glob(".cairn-edit-*"))


def test_edit_file_creates_without_overwriting(tmp_path: Path) -> None:
    tool = EditFileTool(Workspace(tmp_path))
    arguments = {"path": "new/example.py", "old_text": "", "new_text": "print('ok')\n"}

    result = asyncio.run(tool.execute(arguments))

    path = tmp_path / "new" / "example.py"
    assert result.stdout == "Created new/example.py"
    assert path.read_text(encoding="utf-8") == "print('ok')\n"
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(tool.execute(arguments))
    assert path.read_text(encoding="utf-8") == "print('ok')\n"


def test_file_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    for path in ("../outside.txt", str(outside), "link.txt"):
        with pytest.raises(ValueError):
            asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": path}))
        with pytest.raises(ValueError):
            asyncio.run(
                EditFileTool(Workspace(tmp_path)).execute(
                    {"path": path, "old_text": "private", "new_text": "bad"}
                )
            )
    assert outside.read_text(encoding="utf-8") == "private"


def test_file_tools_reject_symlinked_parent_into_git(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    config = git_dir / "config"
    config.write_text(
        "dummy-test-data",
        encoding="utf-8",
    )

    (tmp_path / "alias").symlink_to(
        git_dir,
        target_is_directory=True,
    )

    with pytest.raises(ValueError):
        asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": "alias/config"}))

    with pytest.raises(ValueError):
        asyncio.run(
            EditFileTool(Workspace(tmp_path)).execute(
                {
                    "path": "alias/config",
                    "old_text": "dummy-test-data",
                    "new_text": "changed",
                }
            )
        )

    assert config.read_text(encoding="utf-8") == "dummy-test-data"


def test_edit_file_rejects_creation_through_symlinked_parent(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    (tmp_path / "alias").symlink_to(
        git_dir,
        target_is_directory=True,
    )

    with pytest.raises(ValueError):
        asyncio.run(
            EditFileTool(Workspace(tmp_path)).execute(
                {
                    "path": "alias/new-file",
                    "old_text": "",
                    "new_text": "bad",
                }
            )
        )

    assert not (git_dir / "new-file").exists()


def test_file_tools_reject_direct_git_path(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    config = git_dir / "config"
    config.write_text("dummy-test-data", encoding="utf-8")

    with pytest.raises(ValueError):
        asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": ".git/config"}))
    with pytest.raises(ValueError):
        asyncio.run(
            EditFileTool(Workspace(tmp_path)).execute(
                {
                    "path": ".git/config",
                    "old_text": "dummy-test-data",
                    "new_text": "changed",
                }
            )
        )

    assert config.read_text(encoding="utf-8") == "dummy-test-data"


def test_file_tools_reject_nested_symlink_components(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    config = git_dir / "config"
    config.write_text("dummy-test-data", encoding="utf-8")
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (tmp_path / "alias1").symlink_to(real_dir, target_is_directory=True)
    (real_dir / "alias2").symlink_to(git_dir, target_is_directory=True)

    # Also start from a real directory so checking only the first component fails.
    for parent in ("alias1/alias2", "real_dir/alias2"):
        with pytest.raises(ValueError, match="symlink"):
            asyncio.run(
                ReadFileTool(Workspace(tmp_path)).execute({"path": f"{parent}/config"})
            )
        with pytest.raises(ValueError, match="symlink"):
            asyncio.run(
                EditFileTool(Workspace(tmp_path)).execute(
                    {
                        "path": f"{parent}/config",
                        "old_text": "dummy-test-data",
                        "new_text": "changed",
                    }
                )
            )
        with pytest.raises(ValueError, match="symlink"):
            asyncio.run(
                EditFileTool(Workspace(tmp_path)).execute(
                    {
                        "path": f"{parent}/new/file",
                        "old_text": "",
                        "new_text": "bad",
                    }
                )
            )

    assert config.read_text(encoding="utf-8") == "dummy-test-data"
    assert not (git_dir / "new").exists()
