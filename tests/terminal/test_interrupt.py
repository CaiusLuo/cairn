"""Interrupting a running turn: the transient state and the receipt it leaves.

An interrupt is a state, not a keystroke. Between the request and the completed
cleanup the turn is neither running nor finished, and showing nothing there reads as
a hang. What survives afterwards is a receipt, because a partial answer is discarded
but the fact that a turn stopped early is worth keeping.
"""

from cairn.terminal.interrupt import Interruption


def test_nothing_is_pending_initially() -> None:
    state = Interruption()
    assert not state.pending
    assert state.message() == ""


def test_beginning_marks_the_interrupt_pending() -> None:
    state = Interruption()
    assert state.begin(at=0.0)
    assert state.pending


def test_the_surface_says_it_is_interrupting() -> None:
    state = Interruption()
    state.begin(at=0.0)

    assert state.message() == "Interrupting…"


def test_a_second_request_is_ignored() -> None:
    """A cancellation already in flight must not be requested twice."""
    state = Interruption()
    state.begin(at=0.0)

    assert not state.begin(at=1.0)


def test_settling_completes_the_interrupt() -> None:
    state = Interruption()
    state.begin(at=0.0)

    assert state.settle()
    assert not state.pending
    assert state.settled


def test_no_receipt_before_the_interrupt_completes() -> None:
    state = Interruption()
    state.begin(at=0.0)

    assert state.receipt() == ""


def test_a_settled_interrupt_leaves_a_receipt() -> None:
    state = Interruption()
    state.begin(at=0.0)
    state.settle()

    assert state.receipt() == "Interrupted"


def test_a_normal_turn_leaves_no_receipt() -> None:
    """Settling something that was never interrupted must report nothing."""
    state = Interruption()

    assert not state.settle()
    assert state.receipt() == ""


def test_settling_twice_reports_nothing_the_second_time() -> None:
    """A cancelled turn can raise more than once on its way out."""
    state = Interruption()
    state.begin(at=0.0)
    state.settle()

    assert not state.settle()
    assert state.receipt() == "Interrupted"


def test_resetting_clears_the_previous_interrupt() -> None:
    state = Interruption()
    state.begin(at=0.0)
    state.settle()
    state.reset()

    assert not state.pending
    assert not state.settled
    assert state.receipt() == ""


def test_an_interrupt_after_one_settled_can_begin_again() -> None:
    """The next turn may be interrupted too, once the last one is cleared."""
    state = Interruption()
    state.begin(at=0.0)
    state.settle()
    state.reset()

    assert state.begin(at=5.0)
    assert state.pending
