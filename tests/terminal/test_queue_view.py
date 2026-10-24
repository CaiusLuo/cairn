"""The stack of queued messages shown above the composer.

A queued message is one the operator typed but that has not been delivered. It must
stay visible: a message accepted and then forgotten is indistinguishable from one
that was dropped, and the operator would type it again.

Selection exists for one reason: a message queued a moment ago can become urgent.
The operator points at it and asks for it to steer the running turn instead of
waiting for that turn to end.
"""

from cairn.runtime.turn.steering import PendingMessage, SteerQueue
from cairn.terminal.queue_view import QueueView


def _queue(*texts: str) -> SteerQueue:
    queue = SteerQueue()
    for text in texts:
        queue.push(PendingMessage(text=text, delivery="queue"))
    return queue


def test_nothing_queued_shows_nothing() -> None:
    assert QueueView().rows(SteerQueue()) == []


def test_queued_messages_are_listed_in_order() -> None:
    view = QueueView()
    rows = view.rows(_queue("first", "second", "third"))

    assert [row.text for row in rows] == ["first", "second", "third"]
    assert [row.index for row in rows] == [0, 1, 2]


def test_the_first_message_is_selected_initially() -> None:
    rows = QueueView().rows(_queue("a", "b"))
    assert [row.selected for row in rows] == [True, False]


def test_moving_up_stops_at_the_oldest() -> None:
    view = QueueView()
    queue = _queue("a", "b")
    view.move_up(queue)
    assert view.selected == 0


def test_moving_down_stops_at_the_newest() -> None:
    view = QueueView()
    queue = _queue("a", "b")
    view.move_down(queue)
    view.move_down(queue)
    view.move_down(queue)
    assert view.selected == 1


def test_selection_moves_between_messages() -> None:
    view = QueueView()
    queue = _queue("a", "b", "c")
    view.move_down(queue)
    assert [row.selected for row in view.rows(queue)] == [False, True, False]


def test_steers_do_not_shift_the_selection() -> None:
    """The running turn consumes steers, so indices must count queued only."""
    view = QueueView()
    queue = _queue("queued-a", "queued-b")
    queue.push(PendingMessage(text="a steer", delivery="steer"))

    view.move_down(queue)

    assert [row.text for row in view.rows(queue)] == ["queued-a", "queued-b"]
    assert view.selected == 1


def test_promoting_the_selection_turns_it_into_a_steer() -> None:
    view = QueueView()
    queue = _queue("a", "b")
    view.move_down(queue)

    assert view.promote_selected(queue)
    assert [item.text for item in queue.take_steers()] == ["b"]
    assert [item.text for item in queue.queued()] == ["a"]


def test_promoting_with_nothing_queued_does_nothing() -> None:
    view = QueueView()
    assert not view.promote_selected(SteerQueue())


def test_promoting_moves_the_selection_back_into_range() -> None:
    view = QueueView()
    queue = _queue("a", "b")
    view.move_down(queue)
    view.promote_selected(queue)

    rows = view.rows(queue)
    assert [row.text for row in rows] == ["a"]
    assert view.selected == 0


def test_the_hint_names_both_keys_only_while_running() -> None:
    """Outside a turn both keys just submit, so the hint must not claim otherwise."""
    view = QueueView()
    assert view.hint(running=False) == ""

    running = view.hint(running=True)
    assert "Enter" in running
    assert "Tab" in running


def test_the_hint_says_guide_and_queue() -> None:
    hint = QueueView().hint(running=True)
    assert "guide" in hint.lower()
    assert "queue" in hint.lower()
