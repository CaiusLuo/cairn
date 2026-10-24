"""Reading input while a turn runs, without a second terminal owner.

A turn can take minutes, and an operator who wants to change direction should not
wait for it. That requires reading typed lines while the turn runs — but a
``prompt_toolkit`` application cannot run twice at once, so "start the input, then
start the turn" is not possible.

The resolution is that the input line is *always* running while a session is
interactive, and a submitted line is delivered through a queue. A reader therefore
never competes with the turn for the terminal: it is the same application
throughout, and the turn is just something else that happens to be running.
"""

import asyncio

import pytest

from cairn.terminal.submission import LineQueue, PendingLine


def test_a_submission_is_delivered_once() -> None:
    async def scenario() -> str:
        queue = LineQueue()
        queue.submit("hello")
        return (await queue.next_line()).text

    assert asyncio.run(scenario()) == "hello"


def test_reading_waits_until_a_line_arrives() -> None:
    async def scenario() -> str:
        queue = LineQueue()

        async def submit_later() -> None:
            await asyncio.sleep(0.01)
            queue.submit("late")

        task = asyncio.ensure_future(submit_later())
        line = await queue.next_line()
        await task
        return line.text

    assert asyncio.run(scenario()) == "late"


def test_lines_keep_their_order() -> None:
    async def scenario() -> list[str]:
        queue = LineQueue()
        for text in ("first", "second", "third"):
            queue.submit(text)
        return [(await queue.next_line()).text for _ in range(3)]

    assert asyncio.run(scenario()) == ["first", "second", "third"]


def test_a_blank_line_is_not_a_message() -> None:
    """Blank input keeps the prompt open instead of becoming a turn."""

    async def scenario() -> str:
        queue = LineQueue()
        queue.submit("   ")
        queue.submit("\n")
        queue.submit("real")
        return (await queue.next_line()).text

    assert asyncio.run(scenario()) == "real"


def test_closing_releases_a_waiting_reader() -> None:
    """A session that ends must not leave a reader blocked for ever."""

    async def scenario() -> str:
        queue = LineQueue()

        async def close_later() -> None:
            await asyncio.sleep(0.01)
            queue.close()

        task = asyncio.ensure_future(close_later())
        with pytest.raises(EOFError):
            await queue.next_line()
        await task
        return "released"

    assert asyncio.run(scenario()) == "released"


def test_a_closed_queue_refuses_further_submissions() -> None:
    queue = LineQueue()
    queue.close()
    with pytest.raises(EOFError):
        queue.submit("too late")


def test_the_queue_reports_its_length() -> None:
    queue = LineQueue()
    assert len(queue) == 0
    queue.submit("a")
    queue.submit("b")
    assert len(queue) == 2


def test_a_line_carries_the_delivery_the_operator_asked_for() -> None:
    """The queue carries intent, not just text: the turn needs to know how."""

    async def scenario() -> str:
        queue = LineQueue()
        queue.submit(PendingLine(text="later", delivery="queue"))
        return (await queue.next_line()).delivery

    assert asyncio.run(scenario()) == "queue"


def test_a_plain_submission_defaults_to_starting_work() -> None:
    async def scenario() -> str:
        queue = LineQueue()
        queue.submit("start")
        return (await queue.next_line()).delivery

    assert asyncio.run(scenario()) == "now"
