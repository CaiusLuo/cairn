"""Read one agent skill's full instructions on demand."""

from __future__ import annotations

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.skills import SkillLibrary
from cairn.infra.tools.arguments import function_schema, reject_unknown_keys
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext


class ReadSkillTool:
    """Return a skill body that the system message only advertises.

    Only the name and description travel with every request; the body arrives
    when the model asks for it, so a large skill library costs little until it is
    used. Listing the available names in the error keeps a wrong name cheap to
    correct in the same turn.
    """

    name = "read_skill"
    description = (
        "Read one skill's full instructions. The available skills and what each "
        "is for are listed in the system message; call this only for a skill "
        "whose instructions you are about to follow."
    )

    def __init__(self, library: SkillLibrary) -> None:
        self.library = library

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name exactly as listed in the system message."
                        ),
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        """Check the name argument without reading any skill."""
        reject_unknown_keys(arguments, {"name"}, tool_label="read_skill tool")
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidArguments("name must be a non-empty string")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        name = arguments["name"].strip()
        skill = self.library.read(name)
        if skill is None:
            available = ", ".join(item.name for item in self.library.summaries())
            detail = f" Available skills: {available}." if available else ""
            return ToolResult(
                stdout="",
                stderr=f"Unknown skill: {name}.{detail}",
                exit_code=1,
            )
        return ToolResult(stdout=skill.body, stderr="", exit_code=0)
