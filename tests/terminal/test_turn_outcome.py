"""What a turn does when it stops, and where that decision came from.

A turn can answer, run out of budget, fail, or be stopped by the operator. Only one
of those is an interruption, and telling them apart is not cosmetic: the wrong
answer either hides a real failure behind a cheerful receipt, or turns an operator's
Ctrl-C into a traceback that ends the session.

Cancellation reaches an ``asyncio`` task as a bare ``CancelledError``, which cannot
say who asked for it. These tests drive the real ``_drive_turn`` with the same key
path a terminal uses, so the classification is exercised where it actually matters
rather than asserted against a helper.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import pytest

import cairn.terminal.cli as cli_module
from cairn.core.budget import BudgetReason, RunBudgetExceeded
from cairn.terminal.submission import PendingLine


class _Frame:
    """The part of the input view a turn touches: reading lines, and Ctrl-C.

    A real frame is a prompt_toolkit application; what matters here is its contract,
    so this records what the turn asked of it instead of drawing anything.
    """

    def __init__(self, *lines: str) -> None:
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        for line in lines:
            self._lines.put_nowait(line)
        self.on_interrupt: Callable[[], None] | None = None
        self.running: list[bool] = []

    async def read(self) -> PendingLine:
        return PendingLine(text=await self._lines.get())

    def set_running(self, running: bool) -> None:
        self.running.append(running)

    def press_ctrl_c(self) -> None:
        """Do exactly what the key binding does."""
        assert self.on_interrupt is not None, "no interrupt binding while thinking"
        self.on_interrupt()


class _Display:
    """Records what the display was asked to do, in order."""

    def __init__(self) -> None:
        self.final_text_visible = False
        self.interrupts = 0
        self.settled = 0
        self.closed: list[bool] = []

    def begin_interrupt(self) -> bool:
        self.interrupts += 1
        return True

    def settle_interrupt(self) -> None:
        self.settled += 1

    def close_incomplete(
        self, *, abandoned: bool = False, silent: bool = False
    ) -> None:
        self.closed.append(silent)

    def reset(self) -> None:
        pass

    def observe(self, event: Any) -> bool:
        return True


def _run_drive(
    monkeypatch: pytest.MonkeyPatch,
    turn: Callable[[], Awaitable[str]],
    *,
    frame: _Frame,
    display: _Display,
    press: Callable[[_Frame], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Drive one turn whose body is ``turn``, optionally pressing a key meanwhile."""

    async def run_turn(
        agent: Any, user_input: str, *, budget: Any, steering: Any
    ) -> str:
        return await turn()

    monkeypatch.setattr(cli_module, "run_turn", run_turn)

    async def scenario() -> None:
        async def drive() -> None:
            await cli_module._drive_turn(
                agent=object(),  # type: ignore[arg-type]
                user_input="do the thing",
                steering=object(),  # type: ignore[arg-type]
                frame=frame,  # type: ignore[arg-type]
                streaming=display,  # type: ignore[arg-type]
                durable_session=None,
            )

        if press is None:
            await drive()
            return
        presser = asyncio.create_task(press(frame))
        try:
            await drive()
        finally:
            # The key task is only a driver for the turn; when the turn has been
            # unwound there is nothing left for it to wait for, so it is cancelled
            # and observed rather than awaited indefinitely.
            presser.cancel()
            await asyncio.gather(presser, return_exceptions=True)

    asyncio.run(scenario())


def test_a_turn_that_answers_returns_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _Frame()
    display = _Display()

    async def answer() -> str:
        return "the answer"

    async def scenario() -> str:
        async def run_turn(
            agent: Any, user_input: str, *, budget: Any, steering: Any
        ) -> str:
            return await answer()

        monkeypatch.setattr(cli_module, "run_turn", run_turn)

        return await cli_module._drive_turn(
            agent=object(),  # type: ignore[arg-type]
            user_input="do the thing",
            steering=object(),  # type: ignore[arg-type]
            frame=frame,  # type: ignore[arg-type]
            streaming=display,  # type: ignore[arg-type]
            durable_session=None,
        )

    assert asyncio.run(scenario()) == "the answer"
    assert display.settled == 0


def test_a_failing_turn_propagates_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure must reach the turn loop, not become an interruption."""

    class ProviderError(Exception):
        pass

    async def fail() -> str:
        raise ProviderError("the provider refused")

    with pytest.raises(ProviderError, match="the provider refused"):
        _run_drive(monkeypatch, fail, frame=_Frame(), display=_Display())


def test_an_exhausted_budget_propagates_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exhausted() -> str:
        raise RunBudgetExceeded(reason=BudgetReason.MAX_STEPS, limit=1, used=1)

    with pytest.raises(RunBudgetExceeded):
        _run_drive(monkeypatch, exhausted, frame=_Frame(), display=_Display())


def test_an_operator_interrupt_is_reported_as_an_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C while working: the surface says so and no error escapes."""
    frame = _Frame()
    display = _Display()
    cancelled = asyncio.Event()
    exited = asyncio.Event()

    started = asyncio.Event()

    async def wait_forever() -> str:
        # Record the cancellation where it lands, so the test can tell the turn was
        # actually unwound rather than merely reported as interrupted.
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            exited.set()
        return "unreachable"

    async def press(target: _Frame) -> None:
        # The binding exists as soon as the turn is driven, but pressing it before
        # the turn body has started would cancel a task that never ran. Wait for the
        # body to reach its first await, exactly as a reader waits for the screen.
        while target.on_interrupt is None or not started.is_set():
            await asyncio.sleep(0)
        target.press_ctrl_c()
        for _ in range(20):
            if exited.is_set():
                return
            await asyncio.sleep(0)

    with pytest.raises(cli_module.TurnStopped):
        _run_drive(
            monkeypatch,
            wait_forever,
            frame=frame,
            display=display,
            press=press,
        )

    assert display.interrupts == 1
    assert display.settled == 1
    # The turn body was actually unwound: this is the difference between reporting
    # an interruption and merely refusing to report anything.
    assert exited.is_set()
    assert cancelled.is_set()
    # The binding belonged to that turn and is gone with it.
    assert frame.on_interrupt is None


def test_cleanup_failure_racing_operator_stop_remains_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl-C request cannot hide an unrelated failure during cancellation."""
    frame = _Frame()
    display = _Display()
    started = asyncio.Event()

    async def fail_during_cleanup() -> str:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as exc:
            raise RuntimeError("cleanup failed") from exc
        return "unreachable"

    async def press(target: _Frame) -> None:
        while target.on_interrupt is None or not started.is_set():
            await asyncio.sleep(0)
        target.press_ctrl_c()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run_drive(
            monkeypatch,
            fail_during_cleanup,
            frame=frame,
            display=display,
            press=press,
        )

    assert display.interrupts == 1
    assert display.settled == 0
    assert frame.on_interrupt is None


def test_a_cancellation_nobody_requested_is_not_an_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Something else cancelled the turn; that is a failure, not a receipt."""
    display = _Display()

    async def cancelled() -> str:
        task = asyncio.current_task()
        assert task is not None
        # Cancel from outside, as a shutdown or a superseded turn would.
        asyncio.get_running_loop().call_soon(task.cancel)
        await asyncio.sleep(10)
        return "unreachable"

    with pytest.raises(asyncio.CancelledError, match="without an operator request"):
        _run_drive(monkeypatch, cancelled, frame=_Frame(), display=display)

    # Nothing claimed the operator asked for it.
    assert display.interrupts == 0
    assert display.settled == 0


def test_a_second_ctrl_c_does_not_rerequest_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C is easy to hit twice; the turn unwinds once and reports once."""
    frame = _Frame()
    display = _Display()

    async def wait_forever() -> str:
        await asyncio.sleep(10)
        return "unreachable"

    async def press(target: _Frame) -> None:
        while target.on_interrupt is None:
            await asyncio.sleep(0)
        target.press_ctrl_c()
        target.press_ctrl_c()
        target.press_ctrl_c()

    with pytest.raises(cli_module.TurnStopped):
        _run_drive(
            monkeypatch,
            wait_forever,
            frame=frame,
            display=display,
            press=press,
        )

    assert display.interrupts == 1
    assert display.settled == 1


def test_running_extension_selection_is_retained_for_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.runtime.turn.steering import SteerQueue

    queue = SteerQueue()
    frame = _Frame("[skill:review] [mcp:local] check this")
    notices: list[Any] = []
    monkeypatch.setattr(
        cli_module, "_show_action", lambda action, frame: notices.append(action)
    )

    async def run_turn(
        agent: Any, user_input: str, *, budget: Any, steering: Any
    ) -> str:
        async with asyncio.timeout(2):
            while not steering.queued():
                await asyncio.sleep(0)
        assert not steering.has_pending_steers
        return "first task done"

    monkeypatch.setattr(cli_module, "run_turn", run_turn)
    result = asyncio.run(
        cli_module._drive_turn(
            agent=object(),  # type: ignore[arg-type]
            user_input="first task",
            steering=queue,
            frame=frame,  # type: ignore[arg-type]
            streaming=_Display(),  # type: ignore[arg-type]
            durable_session=None,
        )
    )
    assert result == "first task done"
    assert [line.text for line in queue.queued()] == [
        "[skill:review] [mcp:local] check this"
    ]
    assert notices and "next turn" in notices[0].message
