"""Agent-facing personal-memory tools."""

from __future__ import annotations

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.runtime.agent_memory.service import PersonalMemoryService


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments(f"{name} must be a non-empty string")
    return value.strip()


class _MemoryTool:
    def __init__(self, service: PersonalMemoryService) -> None:
        self.service = service

    @staticmethod
    def _result(text: str, *, ok: bool = True) -> ToolResult:
        return ToolResult(
            stdout=text, stderr="" if ok else text, exit_code=0 if ok else 1
        )


class MemorySearchTool(_MemoryTool):
    name = "memory_search"
    description = "Search the user's personal long-term memory."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        query = arguments.get("query", "")
        if not isinstance(query, str):
            raise InvalidArguments("query must be a string")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        result = self._result
        needle = self.service.redact(str(arguments.get("query", "")).strip()).casefold()
        memory = self.service.store.read_memory_md()
        matches = [
            line.strip() for line in memory.splitlines() if needle in line.casefold()
        ]
        if not matches:
            return result("No matching personal memory.")
        return result("\n".join(matches[:20]))


class MemoryRememberTool(_MemoryTool):
    name = "memory_remember"
    description = (
        "Remember one durable fact or preference explicitly relevant to the user."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        _text(arguments.get("text"), "text")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        try:
            changed = self.service.remember_fact(arguments["text"].strip())
        except (OSError, ValueError) as exc:
            return self._result(f"could not remember: {exc}", ok=False)
        return self._result("remembered" if changed else "already remembered")


class MemoryForgetTool(_MemoryTool):
    name = "memory_forget"
    description = "Forget facts matching the user's explicit deletion request."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        _text(arguments.get("query"), "query")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        try:
            changed = self.service.forget_fact(arguments["query"].strip())
        except (OSError, ValueError) as exc:
            return self._result(f"could not forget: {exc}", ok=False)
        return self._result("forgotten" if changed else "no matching memory")


class MemoryCorrectTool(_MemoryTool):
    name = "memory_correct"
    description = "Correct one remembered fact after the user supplies the replacement."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["old", "new"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        _text(arguments.get("old"), "old")
        _text(arguments.get("new"), "new")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        try:
            changed = self.service.correct_fact(
                arguments["old"].strip(), arguments["new"].strip()
            )
        except (OSError, ValueError) as exc:
            return self._result(f"could not correct: {exc}", ok=False)
        return self._result("corrected" if changed else "no matching memory")
