"""Agent-facing personal-memory tools."""

from __future__ import annotations

import re
from typing import Any

from cairn.agent.models.messages import ToolResult
from cairn.agent.tools.base import InvalidArguments, ToolExecutionContext
from cairn.memory.models import MemorySource
from cairn.memory.service import PersonalMemoryService


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments(f"{name} must be a non-empty string")
    return value.strip()


class _MemoryTool:
    def __init__(self, service: PersonalMemoryService) -> None:
        self.service = service

    def _disabled(self) -> ToolResult | None:
        if not self.service.enabled:
            return self._result("personal memory is disabled", ok=False)
        return None

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
        if disabled := self._disabled():
            return disabled
        query = str(arguments.get("query", "")).strip()
        matches = self.service.search_facts(query)
        if not matches:
            return self._result("No matching personal memory.")
        return self._result(
            "\n".join(
                f"[{source}] {date + ': ' if date else ''}{fact}"
                for source, date, fact in matches
            )
        )


def _clauses(text: str) -> list[str]:
    return [
        " ".join(part.split()).casefold()
        for part in re.split(r"[\u3002\uff01\uff1f!?,\uff0c;\uff1b\n]", text)
        if part.strip()
    ]


def _fact_matches_user_text(fact: str, user_text: str) -> bool:
    """The fact must be a complete clause in the user's own input.

    A keyword such as "记住" is not consent for arbitrary model text, a
    substring inside a negated clause ("不喜欢喝茶") is not a match for
    "喜欢喝茶", and "不要记住这些" is a negative request, not consent.
    """
    target = " ".join(fact.split()).casefold()
    negative = (
        "不要",
        "别记住",
        "不用",
        "无需",
        "不需要",
        "勿",
        "do not",
        "don't",
        "never",
    )
    prefixes = (
        "请记住",
        "帮我记住",
        "记住",
        "记下来",
        "别忘了",
        "please remember",
        "remember",
    )
    for clause in _clauses(user_text):
        if any(marker in clause for marker in negative):
            continue
        for prefix in prefixes:
            if clause.startswith(prefix):
                remainder = clause[len(prefix) :].lstrip(" :\uff1a,\uff0c").strip()
                if remainder == target:
                    return True
    return False


def _negative_memory_request(user_text: str) -> bool:
    text = user_text.casefold()
    patterns = (
        "不要记住",
        "别记住",
        "不用记住",
        "无需记住",
        "不需要记住",
        "勿记住",
        "do not remember",
        "don't remember",
        "never remember",
    )
    return any(pattern in text for pattern in patterns)


class MemoryRememberTool(_MemoryTool):
    name = "memory_remember"
    description = (
        "Remember one durable fact or preference. Set explicit=true only when the "
        "user explicitly asked to remember it; the host verifies the fact against "
        "the trusted user text."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "explicit": {"type": "boolean", "default": False},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - {"text", "explicit"}:
            raise InvalidArguments("memory_remember accepts only text and explicit")
        _text(arguments.get("text"), "text")
        if not isinstance(arguments.get("explicit", False), bool):
            raise InvalidArguments("explicit must be a boolean")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        if disabled := self._disabled():
            return disabled
        trusted = context.trusted_user_text if context is not None else ""
        if _negative_memory_request(trusted):
            return self._result("not remembered; user declined memory")
        try:
            trusted = context.trusted_user_text if context is not None else ""
            explicit_evidence = _fact_matches_user_text(
                arguments["text"].strip(), trusted
            )
            changed = self.service.remember_fact(
                arguments["text"].strip(),
                source=(
                    MemorySource.USER_EXPLICIT
                    if arguments.get("explicit", False) and explicit_evidence
                    else MemorySource.AGENT_INFERRED
                ),
            )
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
        if disabled := self._disabled():
            return disabled
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
