"""Multi-hunk replacement against a single workspace file."""

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

PATCH_FILE_MAX_BYTES = 1024 * 1024  # 1 MiB

# A patch is applied in file order, so a caller cannot make two hunks that
# both claim the same text. Each hunk must be unique on its own.
MAX_HUNKS = 100


class PatchTool:
    name = "patch_file"
    description = (
        "Apply several exact-match replacements to one workspace file in a "
        "single call. Use edit_file for one replacement and write_file to "
        "replace a whole file."
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
                        "description": "Workspace-relative path of the file to patch.",
                    },
                    "patches": {
                        "type": "array",
                        "description": (
                            "Replacements to apply, each against the result "
                            "of the previous one. Every 'old_text' must "
                            "occur exactly once in the file at the time it "
                            "is applied."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["old_text", "new_text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "patches"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(arguments, {"path", "patches"}, tool_label="patch_file")
        workspace_path(self.workspace, arguments)

        patches = arguments.get("patches")
        if not isinstance(patches, list) or not patches:
            raise InvalidArguments("patches must be a non-empty array")
        if len(patches) > MAX_HUNKS:
            raise InvalidArguments(f"patches may contain at most {MAX_HUNKS} hunks")

        for patch in patches:
            if not isinstance(patch, dict):
                raise InvalidArguments("each patch must be an object")
            if patch.keys() - {"old_text", "new_text"}:
                raise InvalidArguments("each patch accepts only old_text and new_text")
            if not isinstance(patch.get("old_text"), str) or not isinstance(
                patch.get("new_text"), str
            ):
                raise InvalidArguments("old_text and new_text must be strings")
            if not patch["old_text"]:
                raise InvalidArguments("old_text must not be empty")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        path = self.workspace.resolve_path(arguments["path"])
        patches = arguments["patches"]

        if not path.is_file():
            raise FileNotFoundError(f"File not found: {arguments['path']}")

        data = path.read_bytes()
        if len(data) > PATCH_FILE_MAX_BYTES:
            raise ValueError(
                f"File exceeds patch_file limit of {PATCH_FILE_MAX_BYTES} bytes; "
                "no changes made"
            )

        text = data.decode("utf-8")
        updated = self._apply_all(text, patches)

        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cairn-patch-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

        relative = path.relative_to(self.workspace.root).as_posix()
        return ToolResult(
            stdout=f"Patched {relative} ({len(patches)} hunk"
            f"{'s' if len(patches) != 1 else ''})",
            exit_code=0,
        )

    def _apply_all(self, text: str, patches: list[dict[str, Any]]) -> str:
        """Apply every hunk, or none at all.

        The whole patch is validated against the text first so a bad later hunk
        cannot leave the caller with a half-patched file: either the file is
        untouched or it carries the complete result.
        """
        pending = list(patches)
        current = text

        while pending:
            patch = pending[0]
            old_text = patch["old_text"]
            new_text = patch["new_text"]

            occurrences = current.count(old_text)
            if occurrences == 0:
                raise ValueError(
                    "No match for patch text after applying earlier hunks: "
                    f"{_preview(old_text)}"
                )
            if occurrences > 1:
                raise ValueError(
                    f"Patch text occurs {occurrences} times after applying earlier "
                    f"hunks; it must be unique: {_preview(old_text)}"
                )

            current = current.replace(old_text, new_text, 1)
            pending.pop(0)

        return current


def _preview(text: str, width: int = 60) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > width:
        return collapsed[: width - 3] + "..."
    return collapsed or "<empty>"
