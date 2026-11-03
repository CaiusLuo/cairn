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
