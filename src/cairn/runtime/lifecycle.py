"""Cooperative ownership of asynchronous work Cairn started on a caller's behalf.

Cancellation in Cairn has one invariant: work the harness owns is allowed to
finish its cleanup even when the caller cancels, and repeated external
cancellation never interrupts that cleanup. The workflows implemented that
settle sequence separately, in two copies that drifted.

This module owns that sequence once. Callers keep their own policy for what a
cancelled or failed outcome *means*; they no longer re-implement how to reach it.
"""

import asyncio
from typing import Any


async def settle_owned(
    *tasks: asyncio.Task[Any], propagate_cancellation: bool = False
) -> list[BaseException | None]:
    """Cancel unfinished owned tasks and observe every outcome to completion.

    Returns each task's failure in argument order, or ``None`` when it finished
    without raising. A cancellation that arrives during cleanup is absorbed so it
    cannot truncate that cleanup, and is re-raised afterwards when
    ``propagate_cancellation`` is set: the work finished, but the caller must
    still learn that it asked to stop.
    """
    if not tasks:
        return []
    # Observe already-finished tasks too, so no exception is left unretrieved.
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()
    for task in tasks:
        if not task.done():
            task.cancel()
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
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return outcomes
