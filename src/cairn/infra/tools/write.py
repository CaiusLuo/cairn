"""Whole-file creation and replacement."""

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.arguments import (
    function_schema,
    reject_unknown_keys,
    workspace_path,
)
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.workspace import Workspace

WRITE_FILE_MAX_BYTES = 1024 * 1024  # 1 MiB


def _read_current_content(path: Path) -> str | None:
    """Read the whole-file content an ``expected_content`` guard compares against.

    Returns None when the current content cannot be established within the
    write_file byte limit or is not UTF-8. A guard cannot be satisfied by such a
    file, so the caller refuses the write instead of overwriting something it
    could not verify.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(WRITE_FILE_MAX_BYTES + 1)
    except OSError:
        return None

    if len(data) > WRITE_FILE_MAX_BYTES:
        return None

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class WriteTool:
    name = "write_file"
    description = (
        "Create a new UTF-8 workspace file, or replace the entire contents of "
        "an existing one. Use edit_file to change part of a file."
    )

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
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
                    "expected_content": {
                        "type": "string",
                        "description": (
                            "Optional guard: the exact current content "
                            "this call expects. A mismatch refuses the write."
                        ),
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(
            arguments,
            {"path", "content", "expected_content"},
            tool_label="write_file",
        )
        path = workspace_path(self.workspace, arguments)

        content = arguments.get("content")
        if not isinstance(content, str):
            raise InvalidArguments("content must be a string")

        if len(content.encode("utf-8")) > WRITE_FILE_MAX_BYTES:
            raise InvalidArguments(
                f"content exceeds write_file limit of {WRITE_FILE_MAX_BYTES} bytes"
            )

        # The guard is opt-in and only concerns an existing file, so a missing
        # file never blocks a write: a creation stays a creation.
        if "expected_content" not in arguments:
            return

        expected_content = arguments["expected_content"]
        if not isinstance(expected_content, str):
            raise InvalidArguments("expected_content must be a string")

        if not path.exists():
            return

        if _read_current_content(path) != expected_content:
            relative = path.relative_to(self.workspace.root).as_posix()
            raise InvalidArguments(
                f"current content of {relative} does not match "
                "expected_content; no changes made"
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
