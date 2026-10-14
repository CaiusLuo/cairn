"""Whole-file creation and replacement."""

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.workspace import Workspace

WRITE_FILE_MAX_BYTES = 1024 * 1024  # 1 MiB


class WriteTool:
    name = "write_file"
    description = (
        "Create a new UTF-8 workspace file, or replace the entire contents of "
        "an existing one. Use edit_file to change part of a file."
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
                            "description": (
                                "Workspace-relative path of the file to create "
                                "or overwrite."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Complete UTF-8 content to write. This replaces "
                                "everything already in the file."
                            ),
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if arguments.keys() - {"path", "content"}:
            raise InvalidArguments("unexpected write_file argument")

        raw_path = arguments.get("path")
        if isinstance(raw_path, str) and "\0" in raw_path:
            raise InvalidArguments("path must not contain NUL")
        try:
            self.workspace.resolve_path(raw_path)
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc

        content = arguments.get("content")
        if not isinstance(content, str):
            raise InvalidArguments("content must be a string")

        if len(content.encode("utf-8")) > WRITE_FILE_MAX_BYTES:
            raise InvalidArguments(
                f"content exceeds write_file limit of {WRITE_FILE_MAX_BYTES} bytes"
            )

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        path = self.workspace.resolve_path(arguments["path"])
        content = arguments["content"]

        existed = path.exists()
        parent = path.parent
        # Mirror edit_file: creating a file may create the directories it needs,
        # so a caller can write a new nested path in one call.
        parent.mkdir(parents=True, exist_ok=True)

        # Write to a sibling temporary file, then rename it into place so an
        # interrupted write cannot leave a truncated file behind.
        fd, temporary = tempfile.mkstemp(dir=parent, prefix=".cairn-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)

            if existed:
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

        action = "Replaced" if existed else "Created"
        return ToolResult(
            stdout=f"{action} {path.relative_to(self.workspace.root).as_posix()}",
            exit_code=0,
        )
