"""Finish Git-owned cleanup without letting repeated cancellation detach it."""

import asyncio
from collections.abc import Coroutine
from typing import Any


async def finish_cleanup[T](operation: Coroutine[Any, Any, T]) -> T:
    """Observe cleanup to completion, then propagate cancellation or failure."""
    task = asyncio.create_task(operation)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:
            break
    if cancelled is not None:
        try:
            task.result()
        except BaseException as exc:
            cancelled.add_note(f"Cleanup failed: {exc!r}")
        raise cancelled
    return task.result()
