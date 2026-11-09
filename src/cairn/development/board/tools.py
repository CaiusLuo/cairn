"""Agent-facing tools for the shared task board."""

from __future__ import annotations

from typing import Any

from cairn.core.models import ToolResult
from cairn.development.board.models import (
    TASK_DESCRIPTION_MAX_LENGTH,
    TASK_DETAIL_MAX_LENGTH,
    TASK_TITLE_MAX_LENGTH,
    TaskBoardStatus,
)
from cairn.development.board.service import TaskBoardService
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext


def _text(value: object, name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments(f"{name} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise InvalidArguments(f"{name} must be at most {max_length} characters")
    return value.strip()


class _TaskTool:
    def __init__(self, service: TaskBoardService) -> None:
        self.service = service

    @staticmethod
    def _result(text: str, *, ok: bool = True) -> ToolResult:
        return ToolResult(
            stdout=text, stderr="" if ok else text, exit_code=0 if ok else 1
        )


class TaskCreateTool(_TaskTool):
    name = "task_create"
    description = (
        "Create a persistent shared task for a local idea or an execution request."
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
                        "title": {"type": "string", "maxLength": TASK_TITLE_MAX_LENGTH},
                        "description": {
                            "type": "string",
                            "maxLength": TASK_DESCRIPTION_MAX_LENGTH,
                        },
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - {"title", "description"}:
            raise InvalidArguments("task_create accepts only title and description")
        _text(arguments.get("title"), "title", max_length=TASK_TITLE_MAX_LENGTH)
        description = arguments.get("description", "")
        if not isinstance(description, str):
            raise InvalidArguments("description must be a string")
        if len(description) > TASK_DESCRIPTION_MAX_LENGTH:
            raise InvalidArguments(
                f"description must be at most {TASK_DESCRIPTION_MAX_LENGTH} characters"
            )

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        try:
            task = await self.service.acreate(
                _text(arguments["title"], "title"),
                str(arguments.get("description", "")).strip(),
            )
        except (OSError, ValueError) as exc:
            return self._result(f"could not create task: {exc}", ok=False)
        return self._result(f"created {task.task_id}: {task.title}")


def _format_task(task: object) -> str:
    lines = [
        f"Task: {task.task_id}",  # type: ignore[attr-defined]
        f"Title: {task.title}",  # type: ignore[attr-defined]
        f"Status: {task.status.value}",  # type: ignore[attr-defined]
        f"Source: {task.source.value}",  # type: ignore[attr-defined]
        f"Project: {task.project or '-'}",  # type: ignore[attr-defined]
        f"Session: {task.session_id or '-'}",  # type: ignore[attr-defined]
    ]
    if task.description:  # type: ignore[attr-defined]
        lines.append(f"Description: {task.description}")  # type: ignore[attr-defined]
    if task.todos:  # type: ignore[attr-defined]
        lines.append("Todos:")
        lines.extend(
            f"  [{'x' if todo.done else ' '}] {todo.todo_id} {todo.text}"
            for todo in task.todos  # type: ignore[attr-defined]
        )
    if task.progress:  # type: ignore[attr-defined]
        lines.append("Progress:")
        lines.extend(f"  - {item}" for item in task.progress)  # type: ignore[attr-defined]
    return "\n".join(lines)


class TaskListTool(_TaskTool):
    name = "task_list"
    description = "List shared tasks and their current status."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if arguments:
            raise InvalidArguments("task_list takes no arguments")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        tasks = await self.service.alist()
        if not tasks:
            return self._result("No shared tasks.")
        return self._result("\n".join(_format_task(task) for task in tasks))


class TaskDetailTool(_TaskTool):
    name = "task_detail"
    description = "Read a shared task's description, status, progress and todos."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) != {"task_id"}:
            raise InvalidArguments("task_detail requires only task_id")
        _text(arguments.get("task_id"), "task_id")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        task = await self.service.aget(arguments["task_id"].strip())
        if task is None:
            return self._result("task not found", ok=False)
        return self._result(_format_task(task))


class TaskUseTool(_TaskTool):
    name = "task_use"
    description = (
        "Select an existing task in the current project and associate it with "
        "this session; this updates task-board metadata without resuming execution."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) != {"task_id"}:
            raise InvalidArguments("task_use requires only task_id")
        _text(arguments.get("task_id"), "task_id")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        try:
            task = await self.service.acontinue(arguments["task_id"].strip())
        except (OSError, ValueError) as exc:
            return self._result(
                f"could not select task: {type(exc).__name__}", ok=False
            )
        if task is None:
            return self._result("task not found", ok=False)
        return self._result(_format_task(task))


class TaskUpdateTool(_TaskTool):
    name = "task_update"
    description = (
        "Update a shared task's status, description, or append a progress note."
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
                        "task_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [s.value for s in TaskBoardStatus],
                        },
                        "description": {
                            "type": "string",
                            "maxLength": TASK_DESCRIPTION_MAX_LENGTH,
                        },
                        "progress": {
                            "type": "string",
                            "maxLength": TASK_DETAIL_MAX_LENGTH,
                        },
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - {"task_id", "status", "description", "progress"}:
            raise InvalidArguments("task_update received an unknown field")
        _text(arguments.get("task_id"), "task_id")
        if "status" in arguments and (
            not isinstance(arguments["status"], str)
            or arguments["status"] not in {status.value for status in TaskBoardStatus}
        ):
            raise InvalidArguments("status must be a valid task status")
        for name, max_length in (
            ("description", TASK_DESCRIPTION_MAX_LENGTH),
            ("progress", TASK_DETAIL_MAX_LENGTH),
        ):
            if name in arguments:
                if not isinstance(arguments[name], str):
                    raise InvalidArguments(f"{name} must be a string")
                if len(arguments[name]) > max_length:
                    raise InvalidArguments(
                        f"{name} must be at most {max_length} characters"
                    )
        if not any(name in arguments for name in ("status", "description", "progress")):
            raise InvalidArguments(
                "task_update requires status, description or progress"
            )

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        status = arguments.get("status")
        try:
            task = await self.service.aupdate(
                arguments["task_id"].strip(),
                status=TaskBoardStatus(status) if status else None,
                description=arguments.get("description"),
                progress=arguments.get("progress"),
            )
        except (OSError, ValueError) as exc:
            return self._result(f"could not update task: {exc}", ok=False)
        if task is None:
            return self._result("task not found", ok=False)
        return self._result(f"updated {task.task_id}")


class TaskTodoTool(_TaskTool):
    name = "task_todo"
    description = "Add a todo to a shared task, or mark an existing todo done/undone."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "text": {
                            "type": "string",
                            "maxLength": TASK_DETAIL_MAX_LENGTH,
                        },
                        "todo_id": {"type": "string"},
                        "done": {"type": "boolean"},
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - {"task_id", "text", "todo_id", "done"}:
            raise InvalidArguments("task_todo received an unknown field")
        _text(arguments.get("task_id"), "task_id")
        has_text = "text" in arguments
        has_todo = "todo_id" in arguments
        if has_text == has_todo:
            raise InvalidArguments("provide exactly one of text or todo_id")
        if has_text:
            _text(arguments.get("text"), "text", max_length=TASK_DETAIL_MAX_LENGTH)
        if has_todo:
            _text(arguments.get("todo_id"), "todo_id")
            if not isinstance(arguments.get("done", False), bool):
                raise InvalidArguments("done must be a boolean")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        task_id = arguments["task_id"].strip()
        if arguments.get("todo_id") is not None:
            task = await self.service.aset_todo_done(
                task_id, str(arguments["todo_id"]), bool(arguments.get("done", False))
            )
        else:
            task = await self.service.aadd_todo(
                task_id, _text(arguments.get("text"), "text")
            )
        if task is None:
            return self._result("task not found", ok=False)
        return self._result(_format_task(task))
