"""CLI `/task` presentation over the shared task-board service."""

from __future__ import annotations

from cairn.core.task_board import TaskBoardItem, TaskBoardStatus
from cairn.infra.task_board.store import TaskBoardBusy, TaskBoardValidationError
from cairn.runtime.task_board.service import TaskBoardService
from cairn.terminal.action import Action, correction, note


def _item_line(task: TaskBoardItem) -> str:
    return f"{task.task_id} [{task.status.value}] {task.title}"


def _details(task: TaskBoardItem) -> str:
    lines = [
        f"Task: {task.task_id}",
        f"Title: {task.title}",
        f"Status: {task.status.value}",
        f"Source: {task.source.value}",
        f"Project: {task.project or '-'}",
        f"Session: {task.session_id or '-'}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.todos:
        lines.append("Todos:")
        lines.extend(
            f"  [{'x' if todo.done else ' '}] {todo.todo_id} {todo.text}"
            for todo in task.todos
        )
    if task.progress:
        lines.append("Progress:")
        lines.extend(f"  - {item}" for item in task.progress)
    if task.issue_url:
        lines.append(f"Issue: {task.issue_url}")
    if task.pr_url:
        lines.append(f"PR: {task.pr_url}")
    return "\n".join(lines)


class TaskBoardController:
    def __init__(self, service: TaskBoardService) -> None:
        self.service = service

    async def render_lines(self) -> tuple[str, ...]:
        task_id = self.service.current_task_id
        if not task_id:
            return ()
        # Read the shared store rather than a service-local cache: another
        # session/service may have added a todo or changed the status.
        task = await self.service.aget(task_id)
        if task is None:
            return ()
        self.service.current_task = task
        lines = [f"TASK {task.task_id} [{task.status.value}] {task.title}"]
        for todo in task.todos[:3]:
            lines.append(f"  [{'x' if todo.done else ' '}] {todo.text}")
        return tuple(lines)

    async def command(self, args: str) -> Action:
        try:
            return await self._command(args)
        except TaskBoardBusy:
            return correction("任务面板暂忙\uff0c请稍后重试。")
        except (TaskBoardValidationError, OSError, ValueError) as exc:
            return correction(f"任务面板操作失败: {exc}")

    async def _command(self, args: str) -> Action:
        parts = args.split()
        verb = parts[0].lower() if parts else "list"
        if verb in {"help", "--help", "-h"}:
            return note(
                "/task list | /task show <id> | /task add <title> | /task use <id> | /task todo <id> <text> | /task done <id> <todo-id> | /task undo <id> <todo-id> | /task status <id> <status> | /task progress <id> <text>"
            )
        if verb == "list":
            tasks = await self.service.alist()
            if not tasks:
                return note("没有共享任务。")
            return note("\n".join(_item_line(task) for task in tasks))
        if verb == "show" and len(parts) == 2:
            task = await self.service.aget(parts[1])
            return (
                note(_details(task)) if task else correction("任务不存在或前缀不唯一。")
            )
        if verb == "add":
            title = " ".join(parts[1:]).strip()
            if not title:
                return correction("用法: /task add <title>")
            task = await self.service.acreate(title)
            self.service.current_task_id = task.task_id
            return note(f"已创建任务 {task.task_id}: {task.title}")
        if verb == "use" and len(parts) == 2:
            task = await self.service.aget(parts[1])
            if task is None:
                return correction("任务不存在或前缀不唯一。")
            self.service.current_task_id = task.task_id
            self.service.current_task = task
            return note(_details(task))
        if verb in {"todo", "done", "undo"} and len(parts) >= 3:
            task_id = parts[1]
            if verb == "todo":
                task = await self.service.aadd_todo(task_id, " ".join(parts[2:]))
            else:
                task = await self.service.aset_todo_done(
                    task_id, parts[2], verb == "done"
                )
            return note(_details(task)) if task else correction("任务或 todo 不存在。")
        if verb == "status" and len(parts) == 3:
            try:
                status = TaskBoardStatus(parts[2])
            except ValueError:
                return correction(f"未知状态: {parts[2]}")
            task = await self.service.aupdate(parts[1], status=status)
            return note(_details(task)) if task else correction("任务不存在。")
        if verb == "progress" and len(parts) >= 3:
            task = await self.service.aupdate(parts[1], progress=" ".join(parts[2:]))
            return note(_details(task)) if task else correction("任务不存在。")
        return correction("未知 /task 子命令; 使用 /task help。")
