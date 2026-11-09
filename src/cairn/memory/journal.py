"""Write settled task notes off the Runtime event loop."""

import asyncio
from collections.abc import Callable

from cairn.memory.service import PersonalMemoryService
from cairn.scheduling.runner import TaskOutcome
from cairn.shared.lifecycle import settle_owned


class TaskJournal:
    def __init__(
        self,
        memory: PersonalMemoryService,
        *,
        owner: Callable[[], str | None],
        notice: Callable[[str], None],
    ) -> None:
        self._memory = memory
        self._owner = owner
        self._notice = notice
        self._queue: asyncio.Queue[tuple[str, TaskOutcome, str | None]] = asyncio.Queue(
            maxsize=64
        )
        self.worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.worker = asyncio.create_task(self._write_notes())

    def enqueue(self, prompt: str, outcome: TaskOutcome, recipient: str | None) -> None:
        try:
            self._queue.put_nowait((prompt, outcome, recipient))
        except asyncio.QueueFull:
            self._notice("记忆日志队列已满, 本次结果仍保留在任务历史中。")

    async def close(self) -> None:
        if self.worker is not None:
            await self._queue.join()
            await settle_owned(self.worker)

    async def _write_notes(self) -> None:
        while True:
            prompt, outcome, recipient = await self._queue.get()
            try:
                if recipient is None or recipient == self._owner():
                    await asyncio.to_thread(
                        self._memory.record_task,
                        prompt=prompt,
                        result=outcome,
                        task_id=outcome.task_id,
                    )
            except Exception as error:
                self._notice("记忆日志未保存: " + type(error).__name__)
            finally:
                self._queue.task_done()
