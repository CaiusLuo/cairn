import codecs
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from cairn.core.models import ToolResult
from cairn.tools.base import ToolExecutionContext
from cairn.workspace.workspace import Workspace

READ_FILE_MAX_LINES = 200
READ_FILE_DISPLAY_CHAR_LIMIT = 12_000

# UTF-8 最多 4 bytes / character。
# 这样仍然允许旧 contract 最多展示 12k Unicode 字符。
READ_FILE_CAPTURE_BYTE_LIMIT = READ_FILE_DISPLAY_CHAR_LIMIT * 4

READ_FILE_CHUNK_SIZE = 4 * 1024

EDIT_FILE_MAX_BYTES = 1024 * 1024  # 1 MiB


def _iter_bounded_line_fragments(
    file: BinaryIO,
) -> Iterator[tuple[bytes, bool]]:
    pending_carriage_return = False

    while True:
        chunk = file.readline(READ_FILE_CHUNK_SIZE)
        if not chunk:
            if pending_carriage_return:
                yield b"\r", True
            return

        index = 0
        if pending_carriage_return:
            if chunk.startswith(b"\n"):
                yield b"\r\n", True
                index = 1
            else:
                yield b"\r", True
            pending_carriage_return = False

        fragment_start = index
        while index < len(chunk):
            byte = chunk[index]
            if byte == ord("\r"):
                if fragment_start < index:
                    yield chunk[fragment_start:index], False
                if index + 1 == len(chunk):
                    pending_carriage_return = True
                    index += 1
                elif chunk[index + 1] == ord("\n"):
                    yield b"\r\n", True
                    index += 2
                else:
                    yield b"\r", True
                    index += 1
                fragment_start = index
            elif byte == ord("\n"):
                yield chunk[fragment_start : index + 1], True
                index += 1
                fragment_start = index
            else:
                index += 1

        if fragment_start < len(chunk):
            yield chunk[fragment_start:], False


def _read_bounded_range(
    file: BinaryIO,
    *,
    start_line: int,
    end_line: int,
    capture_limit: int = READ_FILE_CAPTURE_BYTE_LIMIT,
) -> tuple[bytes, bool, bool]:
    captured = bytearray()
    line_number = 1
    truncated = False
    has_more = False

    fragments = iter(_iter_bounded_line_fragments(file))
    for fragment, line_complete in fragments:
        if start_line <= line_number <= end_line:
            remaining = capture_limit - len(captured)
            if remaining > 0:
                captured.extend(fragment[:remaining])
            if len(fragment) > remaining:
                truncated = True
                break

        if line_complete:
            if line_number >= end_line:
                has_more = next(fragments, None) is not None
                break
            line_number += 1

    return bytes(captured), truncated, has_more


def _read_edit_file(path: Path) -> str:
    with path.open("rb") as file:
        data = file.read(EDIT_FILE_MAX_BYTES + 1)

    if len(data) > EDIT_FILE_MAX_BYTES:
        raise ValueError(
            f"File exceeds edit_file limit of {EDIT_FILE_MAX_BYTES} bytes; "
            "no changes made"
        )

    return data.decode("utf-8")


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _decode_bounded_utf8(data: bytes, *, truncated: bool) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    return decoder.decode(data, final=not truncated)


class ReadFileTool:
    name = "read_file"
    description = "Read up to 200 lines of a UTF-8 workspace file."

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path.",
                        },
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        path = self.workspace.resolve_path(arguments.get("path"))
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 199 if isinstance(start, int) else 0)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end - start >= READ_FILE_MAX_LINES
        ):
            raise ValueError("read_file requires a range of 1 to 200 lines")
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {arguments['path']}")

        with path.open("rb") as file:
            data, byte_truncated, has_more = _read_bounded_range(
                file,
                start_line=start,
                end_line=end,
            )

        content = _decode_bounded_utf8(data, truncated=byte_truncated)

        display_truncated = len(content) > READ_FILE_DISPLAY_CHAR_LIMIT
        if display_truncated:
            content = content[:READ_FILE_DISPLAY_CHAR_LIMIT]

        truncated = byte_truncated or display_truncated

        if truncated:
            content += "\n[read_file output truncated]"
        elif has_more:
            content += "\n[read_file more lines available]"

        return ToolResult(
            stdout=content,
            exit_code=0,
            stdout_truncated=truncated,
        )


class EditFileTool:
    name = "edit_file"
    description = (
        "Replace exactly one old_text in a UTF-8 workspace file. "
        "Set old_text to empty to create a new file; existing files cannot be overwritten."
    )

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path.",
                        },
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        path = self.workspace.resolve_path(arguments.get("path"))
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ValueError("old_text and new_text must be strings")

        creating = old_text == ""
        if creating:
            if path.exists():
                raise ValueError("File already exists; no changes made")
            if _utf8_size(new_text) > EDIT_FILE_MAX_BYTES:
                raise ValueError(
                    f"New file would exceed edit_file limit of "
                    f"{EDIT_FILE_MAX_BYTES} bytes; no changes made"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = new_text
        else:
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {arguments['path']}")
            original = _read_edit_file(path)
            matches = original.count(old_text)
            if matches != 1:
                raise ValueError(f"old_text matched {matches} times; no changes made")

            updated_size = (
                _utf8_size(original) - _utf8_size(old_text) + _utf8_size(new_text)
            )

            if updated_size > EDIT_FILE_MAX_BYTES:
                raise ValueError(
                    f"Edited file would exceed edit_file limit of "
                    f"{EDIT_FILE_MAX_BYTES} bytes; no changes made"
                )

            updated = original.replace(old_text, new_text, 1)

        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cairn-edit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(updated)
            if creating:
                if path.exists():
                    raise ValueError("File already exists; no changes made")
                os.link(temporary, path)
            else:
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        action = "Created" if creating else "Updated"
        return ToolResult(stdout=f"{action} {arguments['path']}", exit_code=0)
