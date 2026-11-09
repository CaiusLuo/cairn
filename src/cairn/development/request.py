"""A model-requested phase change; only the host creates the actual worktree."""

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.arguments import function_schema, reject_unknown_keys
from cairn.infra.tools.base import (
    InvalidArguments,
    ToolExecutionContext,
    WorkspaceOnlyTool,
)


class DevelopmentRequest(WorkspaceOnlyTool):
    name = "begin_development"
    description = "Request an isolated development workspace when the user needs project modifications or command execution. Ordinary conversation and read-only inspection need no worktree. This grants no shell, network or publication permission."

    def __init__(self) -> None:
        self.prompt: str | None = None

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 32000}
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(arguments, {"prompt"}, tool_label=self.name)
        text = arguments.get("prompt")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise InvalidArguments("A bounded development goal is required")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        if self.prompt is not None and self.prompt != arguments["prompt"]:
            return ToolResult(
                exit_code=1, stderr="One development goal is already pending"
            )
        self.prompt = arguments["prompt"]
        return ToolResult(
            exit_code=0,
            stdout="Development requested. The host will prepare the worktree after this conversation turn is committed.",
        )

    def after_tools(self) -> str | None:
        return (
            "Development requested; preparing an isolated workspace with the committed conversation."
            if self.prompt is not None
            else None
        )
