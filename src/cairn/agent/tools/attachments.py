"""Run-scoped access to files the host attached to one conversation turn."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from cairn.agent.models.messages import ToolResult
from cairn.agent.tools.arguments import function_schema, reject_unknown_keys
from cairn.agent.tools.base import InvalidArguments, ToolExecutionContext
from cairn.shared.text_extract import (
    MAX_PDF_PAGES,
    ExtractionUnavailable,
    extract_bytes,
)

MAX_ATTACHMENT_CHARS = 32_000


class AttachmentRef(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def media_type(self) -> str: ...

    @property
    def size(self) -> int: ...


class ReadAttachmentTool:
    """Read one file that the host bound to this Run.

    The tool never accepts a path, only a name from its closed set, so a model
    cannot reach the workspace, the upload staging area, or another Run.
    """

    name = "read_attachment"
    description = (
        "Read a text, PDF, Word, Excel, PowerPoint or HTML file attached to "
        "this conversation turn."
    )

    def __init__(self, root: Path, attachments: tuple[AttachmentRef, ...]) -> None:
        self.root = root.resolve()
        self.attachments = {attachment.name: attachment for attachment in attachments}

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": sorted(self.attachments),
                        "description": "Exact attachment name from the turn's manifest.",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(arguments, {"name"}, tool_label="attachment tool")
        name = arguments.get("name")
        if not isinstance(name, str) or name not in self.attachments:
            raise InvalidArguments("Attachment name is not part of this turn")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        del context
        self.validate(arguments)
        attachment = self.attachments[str(arguments["name"])]
        path = (self.root / attachment.name).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError("Attachment is no longer available")
        text = await asyncio.to_thread(_extract, path, attachment)
        if len(text) > MAX_ATTACHMENT_CHARS:
            text = (
                text[:MAX_ATTACHMENT_CHARS]
                + f"\n[attachment truncated after {MAX_ATTACHMENT_CHARS} characters]"
            )
        return ToolResult(stdout=text, exit_code=0)


def _extract(path: Path, attachment: AttachmentRef) -> str:
    try:
        extracted = extract_bytes(
            attachment.name,
            attachment.media_type,
            attachment.size,
            path.read_bytes(),
            max_chars=MAX_ATTACHMENT_CHARS,
            max_pdf_pages=MAX_PDF_PAGES,
        )
    except ExtractionUnavailable as error:
        # The message already names the attachment and never a path.
        return str(error)
    return extracted.text
