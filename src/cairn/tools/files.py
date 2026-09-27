import os
import stat
import tempfile
from itertools import islice
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.workspace.workspace import Workspace


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

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve_path(arguments.get("path"))
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 199 if isinstance(start, int) else 0)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end - start >= 200
        ):
            raise ValueError("read_file requires a range of 1 to 200 lines")
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {arguments['path']}")
        with path.open("r", encoding="utf-8", newline="") as file:
            content = "".join(islice(file, start - 1, end))
            has_more = bool(file.readline())
        if len(content) > 12_000:
            content = content[:12_000] + "\n[read_file output truncated]"
        elif has_more:
            content += "\n[read_file more lines available]"
        return ToolResult(stdout=content, exit_code=0)


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

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve_path(arguments.get("path"))
        old = arguments.get("old_text")
        new = arguments.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old_text and new_text must be strings")

        creating = old == ""
        if creating:
            if path.exists():
                raise ValueError("File already exists; no changes made")
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = new
        else:
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {arguments['path']}")
            with path.open("r", encoding="utf-8", newline="") as file:
                original = file.read()
            matches = original.count(old)
            if matches != 1:
                raise ValueError(f"old_text matched {matches} times; no changes made")
            updated = original.replace(old, new, 1)

        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cairn-edit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(updated)
            if creating:
                os.link(temporary, path)
            else:
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        action = "Created" if creating else "Updated"
        return ToolResult(stdout=f"{action} {arguments['path']}", exit_code=0)
