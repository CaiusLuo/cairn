"""Messages that arrive while a turn is running.

The rules a reader would notice if they were wrong: a steered message must join the
turn in flight rather than restart it, a queued message must wait for the turn to
end, and neither may be silently lost. The promotion rule is the one the operator
drives by hand — picking a waiting message and asking for it to steer instead.
"""

import asyncio

import pytest

from cairn.runtime.turn.steering import PendingMessage, SteerQueue


def test_an_empty_queue_drains_to_nothing() -> None:
    assert SteerQueue().drain() == []


def test_a_steer_is_delivered_on_drain() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="use the other module", delivery="steer"))
    assert [item.text for item in queue.drain()] == ["use the other module"]


def test_draining_takes_everything_once() -> None:
    """Two steps must never consume the same message."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="first"))
    queue.push(PendingMessage(text="second"))

    assert [item.text for item in queue.drain()] == ["first", "second"]
    assert queue.drain() == []


def test_messages_keep_the_order_they_arrived() -> None:
    queue = SteerQueue()
    for text in ("one", "two", "three"):
        queue.push(PendingMessage(text=text))
    assert [item.text for item in queue.drain()] == ["one", "two", "three"]


def test_blank_input_is_not_accepted() -> None:
    """A blank line is a keystroke, not an instruction."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="   "))
    assert len(queue) == 0


def test_a_queued_message_waits_for_the_turn() -> None:
    """A message addressed to after the turn must not join it."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="later", delivery="queue"))

    assert queue.take_steers() == []
    assert [item.text for item in queue.take_queue()] == ["later"]


def test_draining_takes_steers_and_queue_together() -> None:
    """One list of what the operator said, regardless of when each is delivered."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="now", delivery="steer"))
    queue.push(PendingMessage(text="later", delivery="queue"))

    assert [item.text for item in queue.drain()] == ["now", "later"]


def test_taking_steers_leaves_the_queue_intact() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="now", delivery="steer"))
    queue.push(PendingMessage(text="later", delivery="queue"))

    assert [item.text for item in queue.take_steers()] == ["now"]
    assert [item.text for item in queue.queued()] == ["later"]


def test_taking_queue_leaves_the_steers_intact() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="now", delivery="steer"))
    queue.push(PendingMessage(text="later", delivery="queue"))

    assert [item.text for item in queue.take_queue()] == ["later"]
    assert [item.text for item in queue.drain()] == ["now"]


# -- promotion ------------------------------------------------------------


def test_a_queued_message_can_be_promoted_to_a_steer() -> None:
    """The operator picks a waiting message and asks for it to steer instead."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="later", delivery="queue"))

    assert queue.promote(0)
    assert [item.text for item in queue.take_steers()] == ["later"]


def test_promotion_keeps_the_messages_around_it() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="a", delivery="queue"))
    queue.push(PendingMessage(text="b", delivery="queue"))

    assert queue.promote(1)
    assert [item.text for item in queue.take_steers()] == ["b"]
    assert [item.text for item in queue.queued()] == ["a"]


def test_promoting_an_unknown_position_changes_nothing() -> None:
    """A stale selection must not act on a different message."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="only", delivery="queue"))

    assert not queue.promote(5)
    assert not queue.promote(-1)
    assert [item.text for item in queue.queued()] == ["only"]


def test_promotion_ignores_steers_when_counting_positions() -> None:
    """Indices name queued messages, so a steer in between must not shift them."""
    queue = SteerQueue()
    queue.push(PendingMessage(text="steer-a", delivery="steer"))
    queue.push(PendingMessage(text="queue-b", delivery="queue"))

    assert queue.promote(0)
    assert [item.text for item in queue.take_steers()] == ["steer-a", "queue-b"]


def test_a_queued_message_can_be_discarded() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="drop me", delivery="queue"))

    assert queue.discard(0)
    assert len(queue) == 0


def test_discarding_an_unknown_position_changes_nothing() -> None:
    queue = SteerQueue()
    queue.push(PendingMessage(text="keep", delivery="queue"))

    assert not queue.discard(3)
    assert len(queue) == 1


# -- wake-up --------------------------------------------------------------


def test_an_arrival_wakes_a_waiter() -> None:
    """A turn blocked on a model stream must be wakeable to look for input."""

    async def scenario() -> str:
        queue = SteerQueue()

        async def arrive_later() -> None:
            await asyncio.sleep(0.01)
            queue.push(PendingMessage(text="hello"))

        task = asyncio.ensure_future(arrive_later())
        await queue.wait_for_arrival()
        await task
        return queue.drain()[0].text

    assert asyncio.run(scenario()) == "hello"


def test_draining_clears_the_arrival_signal() -> None:
    """The signal means 'something is waiting', so it must not outlive delivery."""

    async def scenario() -> bool:
        queue = SteerQueue()
        queue.push(PendingMessage(text="hello"))
        queue.drain()
        try:
            await asyncio.wait_for(queue.wait_for_arrival(), timeout=0.05)
        except TimeoutError:
            return True
        return False

    assert asyncio.run(scenario())


def test_the_queue_reports_its_length() -> None:
    queue = SteerQueue()
    assert len(queue) == 0
    queue.push(PendingMessage(text="a"))
    queue.push(PendingMessage(text="b"))
    assert len(queue) == 2


@pytest.mark.parametrize("delivery", ["steer", "queue"])
def test_a_message_reports_whether_it_steers(delivery: str) -> None:
    message = PendingMessage(text="x", delivery=delivery)  # type: ignore[arg-type]
    assert message.steers is (delivery == "steer")
