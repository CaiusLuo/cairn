"""A browser leaving a live stream must not turn cleanup into an ASGI error."""

import asyncio
from pathlib import Path
from typing import Any

from tests.support.control_runtime import control_runtime


def test_client_disconnect_during_close_releases_subscription(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            task = await api.submit("hello", "web", mode="conversation")
            await app.state.runtime.queue.join()
            ready = asyncio.Event()
            connected = False

            async def receive() -> dict[str, Any]:
                nonlocal connected
                if not connected:
                    connected = True
                    return {"type": "websocket.connect"}
                await ready.wait()
                return {"type": "websocket.disconnect", "code": 1001}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "websocket.send":
                    ready.set()
                elif message["type"] == "websocket.close":
                    raise OSError("peer already closed")

            scope = {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "scheme": "ws",
                "path": f"/api/runtime/tasks/{task['task_id']}/stream",
                "query_string": b"",
                "root_path": "",
                "server": ("127.0.0.1", 8777),
                "client": ("127.0.0.1", 12345),
                "headers": [
                    (b"host", b"127.0.0.1:8777"),
                    (b"authorization", b"Bearer private-test-token"),
                ],
            }
            await asyncio.wait_for(app(scope, receive, send), 3)
            assert not app.state.runtime.subscribers

    asyncio.run(scenario())
