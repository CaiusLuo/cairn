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

    path.write_text("x" * 1_000_000, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert len(result.stdout) < 13_000
    assert result.stdout.endswith("[read_file output truncated]")
    with pytest.raises(ValueError, match="1 to 200 lines"):
        asyncio.run(tool.execute({"path": "example.py", "end_line": 201}))

    path.write_text("line\n" * 201, encoding="utf-8")
    result = asyncio.run(tool.execute({"path": "example.py"}))
    assert result.stdout.count("line\n") == 200
    assert result.stdout.endswith("[read_file more lines available]")
    path.write_text("skip\n" * 10_000 + "TARGET\n", encoding="utf-8")
    result = asyncio.run(
        tool.execute({"path": "example.py", "start_line": 10_001, "end_line": 10_001})
    )
    assert result.stdout == "TARGET\n"


def test_read_file_stops_scanning_selected_content_after_truncation() -> None:
    capture_limit = READ_FILE_CHUNK_SIZE + 1
    stream = RecordingBytesIO(b"a" * 1_000_000)

    data, truncated, _ = _read_bounded_range(
        stream,
        start_line=1,
        end_line=1,
        capture_limit=capture_limit,
    )

    assert data == b"a" * capture_limit
    assert truncated is True
    assert stream.tell() == READ_FILE_CHUNK_SIZE * 2
    assert len(stream.readline_sizes) == 2
    assert all(size == READ_FILE_CHUNK_SIZE for size in stream.readline_sizes)


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


def test_read_file_preserves_cr_and_crlf_line_boundaries(tmp_path: Path) -> None:
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
    split_crlf = tmp_path / "split-crlf.txt"
    split_crlf.write_bytes(b"a" * (READ_FILE_CHUNK_SIZE - 1) + b"\r\nTARGET\r\n")
    split_result = asyncio.run(
        ReadFileTool(Workspace(tmp_path)).execute(
            {"path": "split-crlf.txt", "start_line": 2, "end_line": 2}
        )
    )
    assert split_result.stdout == "TARGET\r\n"


def test_read_file_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"invalid:\xff\n")

    with pytest.raises(UnicodeDecodeError):
        asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": "invalid.txt"}))


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


def test_edit_file_enforces_utf8_byte_limit_without_mutation(tmp_path: Path) -> None:
    tool = EditFileTool(Workspace(tmp_path))
    oversized = "x" * (EDIT_FILE_MAX_BYTES + 1)
    existing = tmp_path / "existing.txt"
    existing.write_text("kept", encoding="utf-8")

    with pytest.raises(ValueError, match="would exceed edit_file limit"):
        asyncio.run(
            tool.execute(
                {"path": "existing.txt", "old_text": "kept", "new_text": oversized}
            )
        )
    assert existing.read_text(encoding="utf-8") == "kept"

    large_existing = tmp_path / "large.txt"
    original = b"a" * (EDIT_FILE_MAX_BYTES + 1)
    large_existing.write_bytes(original)
    with pytest.raises(ValueError, match="exceeds edit_file limit"):
        asyncio.run(
            tool.execute({"path": "large.txt", "old_text": "a", "new_text": "b"})
        )
    assert large_existing.read_bytes() == original

    oversized_create = tmp_path / "oversized-create.txt"
    with pytest.raises(ValueError, match="would exceed edit_file limit"):
        asyncio.run(
            tool.execute(
                {"path": "oversized-create.txt", "old_text": "", "new_text": oversized}
            )
        )
    assert not oversized_create.exists()

    utf8_path = tmp_path / "utf8.txt"
    multibyte = "你" * (EDIT_FILE_MAX_BYTES // 3 + 1)
    with pytest.raises(ValueError, match="would exceed edit_file limit"):
        asyncio.run(
            tool.execute({"path": "utf8.txt", "old_text": "", "new_text": multibyte})
        )
    assert not utf8_path.exists()


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
    git_config = tmp_path / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text("keep", encoding="utf-8")

    for path in ("../outside.txt", str(outside), "link.txt", ".git/config"):
        with pytest.raises(ValueError):
            asyncio.run(ReadFileTool(Workspace(tmp_path)).execute({"path": path}))
        with pytest.raises(ValueError):
            asyncio.run(
                EditFileTool(Workspace(tmp_path)).execute(
                    {"path": path, "old_text": "private", "new_text": "bad"}
                )
            )
    assert outside.read_text(encoding="utf-8") == "private"
    assert git_config.read_text(encoding="utf-8") == "keep"


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
