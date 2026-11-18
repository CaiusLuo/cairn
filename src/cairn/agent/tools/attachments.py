"""Run-scoped access to files the host attached to one conversation turn."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from pypdf import PdfReader

from cairn.agent.models.messages import ToolResult
from cairn.agent.tools.arguments import function_schema, reject_unknown_keys
from cairn.agent.tools.base import InvalidArguments, ToolExecutionContext

MAX_ATTACHMENT_CHARS = 32_000
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".html",
        ".json",
        ".log",
        ".markdown",
        ".md",
        ".py",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    }
)


class AttachmentRef(Protocol):
    name: str
    media_type: str
    size: int


class ReadAttachmentTool:
    """Read one file that the host bound to this Run.

    The tool never accepts a path, only a name from its closed set, so a model
    cannot reach the workspace, the upload staging area, or another Run.
    """

    name = "read_attachment"
    description = "Read a text or PDF file attached to this conversation turn."

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
        if arguments.get("name") not in self.attachments:
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
        text = _extract(path, attachment)
        if len(text) > MAX_ATTACHMENT_CHARS:
            text = (
                text[:MAX_ATTACHMENT_CHARS]
                + f"\n[attachment truncated after {MAX_ATTACHMENT_CHARS} characters]"
            )
        return ToolResult(stdout=text, exit_code=0)


def _extract(path: Path, attachment: AttachmentRef) -> str:
    media = attachment.media_type
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if media == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(data, attachment)
    if (
        media.startswith("text/")
        or media in _TEXT_MEDIA_TYPES
        or suffix in _TEXT_SUFFIXES
    ):
        return data.decode("utf-8", errors="replace")
    if media.startswith("image/"):
        return (
            f"Image attachment {attachment.name} ({media}, {attachment.size} bytes). "
            "This terminal stores the image but cannot read its pixels."
        )
    return (
        f"Binary attachment {attachment.name} ({media}, {attachment.size} bytes). "
        "Only text and PDF attachments can be read."
    )


def _extract_pdf(data: bytes, attachment: AttachmentRef) -> str:
    try:
        reader = PdfReader(BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:50])
    except Exception:
        return f"PDF attachment {attachment.name} could not be parsed as text."
    if not text.strip():
        return (
            f"PDF attachment {attachment.name} has no extractable text; "
            "it may contain only scanned images."
        )
    return text
