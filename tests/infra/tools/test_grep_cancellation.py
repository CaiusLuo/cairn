"""A cancelled grep retains ownership until its blocking search has finished."""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from cairn.agent.models.messages import ToolResult
from cairn.agent.tools.grep import GrepTool
from cairn.agent.workspace.workspace import Workspace


@pytest.mark.parametrize("worker_fails", [False, True])
def test_grep_settles_search_under_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_fails: bool
) -> None:
    (tmp_path / "ordinary.py").write_text("MARKER = 1\n", encoding="utf-8")
    tool = GrepTool(Workspace(tmp_path))

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        original_search = tool._search

        def blocking_search(*args: Any) -> ToolResult:
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release the search worker")
                if worker_fails:
                    raise OSError("search failed during cancellation")
                return original_search(*args)
            finally:
                finished.set()

        monkeypatch.setattr(tool, "_search", blocking_search)
        running = asyncio.create_task(tool.execute({"pattern": "MARKER"}))
        try:
            await asyncio.wait_for(started.wait(), 5)
            running.cancel("primary cancellation")
            await asyncio.sleep(0)
            running.cancel("repeated cancellation")
            await asyncio.sleep(0)

            assert not running.done(), "cancelled grep must retain its search worker"
            assert not finished.is_set()

            release.set()
            with pytest.raises(asyncio.CancelledError, match="primary cancellation"):
                await running

            assert finished.is_set()
            assert unhandled == []
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
