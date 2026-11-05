"""A background observation must survive message admission by the owner."""

import asyncio
import threading
from pathlib import Path
from typing import Any

from cairn.infra.wechat.state import WechatState


def test_background_queue_observation_survives_new_message(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "state")
    state.record_received("old", sender="owner", context_token="ctx", text="old")
    state.mark("old", "queued")

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        class PausedRecord(dict[str, Any]):
            def get(self, key: Any, default: Any = None) -> Any:
                if key == "state":
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise TimeoutError("test did not release observation")
                return super().get(key, default)

        state.load()["messages"]["old"] = PausedRecord(state.get_record("old") or {})
        observing = asyncio.create_task(
            asyncio.to_thread(
                state.records_in_states, frozenset({"queued", "queueing"})
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            # Admission runs on the owner while the background query is in flight.
            assert state.record_received(
                "new", sender="owner", context_token="ctx", text="new"
            )
            state.mark("new", "queueing")
            release.set()
            rows = await observing
            assert [key for key, _record in rows] == ["old"]
        finally:
            release.set()
            await asyncio.gather(observing, return_exceptions=True)

    asyncio.run(scenario())
    # Observation neither drops the new fact nor changes persisted queue order.
    reopened = WechatState(tmp_path / "state")
    assert [
        key
        for key, _record in reopened.records_in_states(
            frozenset({"queued", "queueing"})
        )
    ] == ["old", "new"]
