"""Supervising a running turn while the operator types.

The contract these tests pin is the one an operator feels: a message typed while
work runs either waits for the turn or redirects it, and a redirect ends the turn
it interrupted rather than rewriting it.
"""

import asyncio
from collections.abc import Callable, Coroutine

import pytest

from cairn.runtime.turn.supervisor import QueuedMessage, TurnQueue, supervise_turn


def _is_interrupt(text: str) -> bool:
    return text.startswith("!")


def _scripted(lines: list[str]) -> Callable[[], Coroutine[object, object, str]]:
    """A reader that yields each scripted line, then blocks for ever."""
    pending = list(lines)

    async def read() -> str:
        if not pending:
            # Nothing scripted left: block so the turn wins the race.
            await asyncio.sleep(3600)
        await asyncio.sleep(0)
        return pending.pop(0)

    return read


def test_a_finished_turn_returns_its_response() -> None:
    async def run(_: str) -> str:
        return "done"

    result = asyncio.run(
        supervise_turn(
            run,
            "start",
            queue=TurnQueue(),
            wait_for_input=_scripted([]),
            is_interrupt=_is_interrupt,
        )
    )

    assert result.response == "done"
    assert not result.interrupted
    assert result.queued == ()


def test_a_queued_message_waits_for_the_turn() -> None:
    """A non-interrupt line is delivered after the turn, not into it."""
    started = asyncio.Event()

    async def run(_: str) -> str:
        started.set()
        await asyncio.sleep(0.02)
        return "finished"

    result = asyncio.run(
        supervise_turn(
            run,
            "start",
            queue=TurnQueue(),
            wait_for_input=_scripted(["next question"]),
            is_interrupt=_is_interrupt,
        )
    )

    assert result.response == "finished"
    assert not result.interrupted
    assert result.queued == (QueuedMessage(text="next question", mode="queue"),)


def test_an_interrupt_ends_the_turn_and_becomes_guidance() -> None:
    """`!` redirects: the running turn is cancelled and its text is carried."""
    cancelled: list[int] = []

    async def run(_: str) -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return "never"

    result = asyncio.run(
        supervise_turn(
            run,
            "long task",
            queue=TurnQueue(),
            wait_for_input=_scripted(["! stop, use the other module"]),
            is_interrupt=_is_interrupt,
        )
    )

    assert result.interrupted
    assert result.interrupted_by == "! stop, use the other module"
    assert result.response is None
    assert cancelled == [1]


def test_several_queued_messages_keep_their_order() -> None:
    async def run(_: str) -> str:
        await asyncio.sleep(0.03)
        return "ok"

    reader = _scripted(["first", "second", "third"])
    result = asyncio.run(
        supervise_turn(
            run,
            "start",
            queue=TurnQueue(),
            wait_for_input=reader,
            is_interrupt=_is_interrupt,
        )
    )

    assert [message.text for message in result.queued] == ["first", "second", "third"]
    assert all(message.mode == "queue" for message in result.queued)


def test_an_interrupt_discards_nothing_already_queued() -> None:
    """Queued text is handed back so the caller can decide what to do with it."""

    async def run(_: str) -> str:
        await asyncio.sleep(0.05)
        return "never"

    reader = _scripted(["queued first", "! redirect"])
    result = asyncio.run(
        supervise_turn(
            run,
            "start",
            queue=TurnQueue(),
            wait_for_input=reader,
            is_interrupt=_is_interrupt,
        )
    )

    assert result.interrupted_by == "! redirect"
    assert [message.text for message in result.queued] == ["queued first"]


def test_the_reader_is_cancelled_when_the_turn_finishes() -> None:
    """A reader must not outlive the turn it was watching."""

    async def run(_: str) -> str:
        return "quick"

    finished = asyncio.Event()

    async def read() -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            finished.set()
            raise
        return ""

    asyncio.run(
        supervise_turn(
            run,
            "start",
            queue=TurnQueue(),
            wait_for_input=read,
            is_interrupt=_is_interrupt,
        )
    )

    assert finished.is_set()


def test_a_failing_turn_propagates_its_error() -> None:
    async def run(_: str) -> str:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(
            supervise_turn(
                run,
                "start",
                queue=TurnQueue(),
                wait_for_input=_scripted([]),
                is_interrupt=_is_interrupt,
            )
        )


def test_the_queue_reports_whether_an_interrupt_is_pending() -> None:
    queue = TurnQueue()
    assert not queue.interrupted
    queue.push(QueuedMessage(text="later", mode="queue"))
    assert not queue.interrupted
    queue.push(QueuedMessage(text="! now", mode="interrupt"))
    assert queue.interrupted


def test_draining_returns_every_waiting_message_in_order() -> None:
    queue = TurnQueue()
    queue.push(QueuedMessage(text="later", mode="queue"))
    queue.push(QueuedMessage(text="! now", mode="interrupt"))

    drained = queue.drain()
    assert [(m.text, m.mode) for m in drained] == [
        ("later", "queue"),
        ("! now", "interrupt"),
    ]
