import asyncio
import json
from pathlib import Path
from typing import Any, cast

from cairn.terminal.wechat.control import WechatControlServer


def test_control_socket_status_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = WechatControlServer(
        path,
        lambda method: {"method": method, "pid": 7, "queued": 1},
    )

    async def scenario() -> dict[str, Any]:
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'{"method":"status"}\n')
            await writer.drain()
            payload = cast(
                dict[str, Any], json.loads((await reader.readline()).decode())
            )
            writer.close()
            await writer.wait_closed()
            return payload
        finally:
            await server.close()

    payload = asyncio.run(scenario())
    assert payload["ok"] is True
    assert cast(dict[str, Any], payload["result"])["method"] == "status"
    assert not path.exists()


from pathlib import Path

from cairn.terminal.wechat.session import WechatSessionController


def test_controller_reads_control_status(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    server = WechatControlServer(
        state_dir / "control.sock",
        lambda _method: {"pid": 9, "session_id": "session-x", "queued": 2, "polls": 4},
    )

    async def scenario() -> dict[str, object] | None:
        await server.start()
        try:
            controller = WechatSessionController(
                workspace=workspace,
                state_dir=state_dir,
                task_executor=lambda *args: None,  # type: ignore[arg-type]
                emit=lambda text: None,
            )
            return await asyncio.to_thread(controller._control_status)
        finally:
            await server.close()

    result = asyncio.run(scenario())
    assert result is not None
    assert result["session_id"] == "session-x"
    assert result["queued"] == 2
