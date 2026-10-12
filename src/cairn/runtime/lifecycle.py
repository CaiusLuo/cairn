"""Cooperative ownership of asynchronous work Cairn started on a caller's behalf.

Cancellation in Cairn has one invariant: work the harness owns is allowed to
finish its cleanup even when the caller cancels, and repeated external
cancellation never interrupts that cleanup.

Two different policies live on top of that invariant, and they are not
interchangeable. *Settling* cancels unfinished work and then waits for it, which
is what a caller does when it is abandoning the operation. *Awaiting* only waits,
which is what a caller does when the remaining work is the cleanup itself and
cancelling it would skip the very guarantee being upheld. Both absorb repeated
external cancellation in the same way, and that mechanism lives here once.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SettleOutcome:
    """What one settle observed.

    ``failures`` holds each task's failure in argument order, or ``None`` when it
    finished without raising. ``cancellation`` is the first external cancellation
    that arrived while the owned work was being observed, or ``None``; a call site
    that must not lose a late stop reads it instead of relying on a default.
    """

    failures: tuple[BaseException | None, ...]
    cancellation: asyncio.CancelledError | None


async def _observe_owned(
    tasks: Sequence[asyncio.Task[Any]],
) -> tuple[list[BaseException | None], asyncio.CancelledError | None]:
    """Wait for every task to finish, absorbing repeated external cancellation."""
    # Observe already-finished tasks too, so no exception is left unretrieved.
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()
    completion = asyncio.gather(*tasks, return_exceptions=True)
    cancellation: asyncio.CancelledError | None = None
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    outcomes = [
        outcome if isinstance(outcome, BaseException) else None
        for outcome in completion.result()
    ]
    return outcomes, cancellation


async def await_owned(*tasks: asyncio.Task[Any]) -> asyncio.CancelledError | None:
    """Wait for owned work to finish without cancelling it.

    Use this when the remaining work *is* the cleanup that must not be skipped.
    Returns the first external cancellation observed while waiting, or ``None``.
    """
    if not tasks:
        return None
    _, cancellation = await _observe_owned(tasks)
    return cancellation


async def settle_owned(
    *tasks: asyncio.Task[Any], propagate_cancellation: bool = False
) -> SettleOutcome:
    """Cancel unfinished owned tasks and observe every outcome to completion.

    A cancellation that arrives during cleanup is absorbed so it cannot truncate
    that cleanup, and is re-raised afterwards when ``propagate_cancellation`` is
    set: the work finished, but the caller must still learn that it asked to
    stop. A call site that must record rather than re-raise a late cancellation
    reads ``SettleOutcome.cancellation`` instead.
    """
    if not tasks:
        return SettleOutcome((), None)
    for task in tasks:
        if not task.done():
            task.cancel()
    outcomes, cancellation = await _observe_owned(tasks)
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return SettleOutcome(tuple(outcomes), cancellation)
