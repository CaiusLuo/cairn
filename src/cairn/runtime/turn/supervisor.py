"""Running one turn while still reading the operator's next message.

A turn can take minutes, and an operator who wants to change direction should not
have to wait for it to finish. This module owns that window: it runs the turn,
accepts messages typed while it runs, and decides — from what the operator typed —
whether a message waits for the turn or interrupts it and becomes the guidance
for the next one.

The turn itself is untouched: an interruption cancels the current turn and starts
a new one whose user message is the operator's guidance. That keeps
``run_turn`` the only Agent turn and keeps the conversation a sequence of real
turns, so a cancelled turn settles through its existing recovery path and the
durable checkpoint records the interruption rather than a rewritten history.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """One message typed while a turn was running.

    ``mode`` records what the operator asked for, not what the implementation
    happened to do, so a message can be reported back accurately.
    """

    text: str
    mode: Literal["queue", "interrupt"]


class TurnQueue:
    """Messages typed while a turn runs.

    A queued message is delivered after the running turn finishes. An interrupt
    is delivered by cancelling the turn: the message then becomes the guidance of
    the next one.
    """

    def __init__(self) -> None:
        self._pending: list[QueuedMessage] = []

    def push(self, message: QueuedMessage) -> None:
        self._pending.append(message)

    def drain(self) -> list[QueuedMessage]:
        """Everything waiting, oldest first. Clears the queue."""
        waiting = list(self._pending)
        self._pending.clear()
        return waiting

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def interrupted(self) -> bool:
        """Whether an interrupt arrived and has not been consumed yet."""
        return any(message.mode == "interrupt" for message in self._pending)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What one supervised turn produced.

    ``response`` is ``None`` when the turn was interrupted before it produced an
    answer, which is a normal outcome rather than an error: the operator chose to
    redirect. ``interrupted_by`` carries the guidance that caused it so the caller
    can open the next turn with it.
    """

    response: str | None
    interrupted_by: str | None = None
    queued: tuple[QueuedMessage, ...] = ()

    @property
    def interrupted(self) -> bool:
        return self.interrupted_by is not None


async def supervise_turn(
    run: Callable[[str], Coroutine[object, object, str]],
    user_input: str,
    *,
    queue: TurnQueue,
    wait_for_input: Callable[[], Coroutine[object, object, str]],
    is_interrupt: Callable[[str], bool],
) -> TurnResult:
    """Run one turn, watching for an interrupt while it runs.

    ``run`` performs the turn. ``wait_for_input`` reads the next line the operator
    types, and ``is_interrupt`` decides whether that line redirects the turn or
    waits for it. Reading is cancelled when the turn finishes, so a reader never
    outlives the turn it was watching.
    """
    task = asyncio.create_task(run(user_input))
    reader = asyncio.create_task(wait_for_input())
    guidance: str | None = None
    try:
        while True:
            done, _ = await asyncio.wait(
                {task, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                queued = tuple(queue.drain())
                return TurnResult(
                    response=await task,
                    interrupted_by=guidance,
                    queued=queued,
                )
            if reader in done:
                try:
                    typed = reader.result()
                except (EOFError, KeyboardInterrupt):
                    typed = ""
                if typed and is_interrupt(typed):
                    # The guidance replaces the turn's remaining work; the turn
                    # settles through its own cancellation path.
                    guidance = typed
                    task.cancel()
                    queued = tuple(queue.drain())
                    return TurnResult(
                        response=None, interrupted_by=guidance, queued=queued
                    )
                if typed:
                    queue.push(QueuedMessage(text=typed, mode="queue"))
                reader = asyncio.create_task(wait_for_input())
    finally:
        for pending in (task, reader):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, reader, return_exceptions=True)
