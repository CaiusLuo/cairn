"""Application service for the shared task board."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from cairn.core.task_board import TaskBoardItem, TaskBoardStatus, TaskSource
from cairn.infra.task_board.store import TaskBoardStore
from cairn.runtime.lifecycle import await_owned


class TaskBoardService:
    def __init__(
        self,
        store: TaskBoardStore,
        *,
        project: str = "",
        session_id: str = "",
        source: TaskSource = TaskSource.LOCAL,
    ) -> None:
        self.store = store
        self.project = project
        self.session_id = session_id
        self.source = source
        self.current_task_id: str | None = None
        self.current_task: TaskBoardItem | None = None

    async def _call(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as primary:
            # The sync store may already be writing. Wait for that owned write
            # to settle instead of leaking a background mutation.
            late = await await_owned(task)
            try:
                task.result()
            except asyncio.CancelledError:
                primary.add_note("Task board operation was cancelled while settling.")
            except Exception as exc:
                primary.add_note(
                    f"Task board operation failed while settling: {type(exc).__name__}."
                )
            if late is not None:
                primary.add_note(
                    "Another cancellation arrived while task board work settled."
                )
            raise

    async def project_execution(self, item: TaskBoardItem) -> TaskBoardItem:
        return cast(TaskBoardItem, await self._call(self.store.project_execution, item))

    async def acreate(self, title: str, description: str = "") -> TaskBoardItem:
        return cast(TaskBoardItem, await self._call(self.create, title, description))

    async def alist(self) -> tuple[TaskBoardItem, ...]:
        return cast(tuple[TaskBoardItem, ...], await self._call(self.list))

    async def aget(self, task_id: str) -> TaskBoardItem | None:
        return cast(TaskBoardItem | None, await self._call(self.get, task_id))

    async def aupdate(self, task_id: str, **kwargs: Any) -> TaskBoardItem | None:
        return cast(
            TaskBoardItem | None, await self._call(self.update, task_id, **kwargs)
        )

    async def amove_status(
        self, task_id: str, expected_revision: int, status: TaskBoardStatus
    ) -> TaskBoardItem | None:
        return cast(
            TaskBoardItem | None,
            await self._call(
                self.store.update_status_if_revision,
                task_id,
                expected_revision,
                status,
            ),
        )

    async def aadd_todo(self, task_id: str, text: str) -> TaskBoardItem | None:
        return cast(
            TaskBoardItem | None, await self._call(self.add_todo, task_id, text)
        )

    async def aset_todo_done(
        self, task_id: str, todo_id: str, done: bool
    ) -> TaskBoardItem | None:
        return cast(
            TaskBoardItem | None,
            await self._call(self.set_todo_done, task_id, todo_id, done),
        )

    def create(self, title: str, description: str = "") -> TaskBoardItem:
        task = self.store.create(
            title=title,
            description=description,
            project=self.project,
            source=self.source,
            session_id=self.session_id,
        )
        self.current_task_id = task.task_id
        self.current_task = task
        return task

    def list(self) -> tuple[TaskBoardItem, ...]:
        return tuple(
            sorted(self.store.load(), key=lambda item: item.created_at, reverse=True)
        )

    def get(self, task_id: str) -> TaskBoardItem | None:
        prefix = task_id.strip()
        if not prefix:
            return None
        matches = [
            task for task in self.store.load() if task.task_id.startswith(prefix)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def update(
        self,
        task_id: str,
        *,
        status: TaskBoardStatus | None = None,
        title: str | None = None,
        description: str | None = None,
        progress: str | None = None,
        issue_url: str | None = None,
        pr_url: str | None = None,
    ) -> TaskBoardItem | None:
        task = self.get(task_id)
        if task is None:
            return None
        updated = self.store.update(
            task.task_id,
            status=status,
            title=title,
            description=description,
            progress=progress,
            issue_url=issue_url,
            pr_url=pr_url,
        )
        if updated is not None and self.current_task_id == updated.task_id:
            self.current_task = updated
        return updated

    async def acontinue(self, task_id: str) -> TaskBoardItem | None:
        return cast(
            TaskBoardItem | None,
            await self._call(self.continue_task, task_id),
        )

    def continue_task(self, task_id: str) -> TaskBoardItem | None:
        """Select this project's task and associate its board metadata only."""
        task = self.get(task_id)
        if task is None or task.project != self.project or not self.session_id:
            return None
        updated = self.store.update(task.task_id, session_id=self.session_id)
        if updated is not None:
            self.current_task_id = updated.task_id
            self.current_task = updated
        return updated

    def add_todo(self, task_id: str, text: str) -> TaskBoardItem | None:
        task = self.get(task_id)
        updated = None if task is None else self.store.add_todo(task.task_id, text)
        if updated is not None and self.current_task_id == updated.task_id:
            self.current_task = updated
        return updated

    def set_todo_done(
        self, task_id: str, todo_id: str, done: bool
    ) -> TaskBoardItem | None:
        task = self.get(task_id)
        updated = (
            None
            if task is None
            else self.store.set_todo_done(task.task_id, todo_id, done)
        )
        if updated is not None and self.current_task_id == updated.task_id:
            self.current_task = updated
        return updated
