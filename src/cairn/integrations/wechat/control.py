"""Small Unix-socket control plane for the detached WeChat owner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


class WechatControlServer:
    """Serve status/tasks/chat snapshots over a 0600 Unix socket."""

    def __init__(self, path: Path, snapshot: Callable[[str], dict[str, Any]]) -> None:
        self.path = path
        self._snapshot = snapshot
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        with contextlib.suppress(OSError):
            self.path.unlink()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            request = json.loads(raw.decode("utf-8"))
            method = request.get("method") if isinstance(request, dict) else None
            if method not in {"status", "tasks", "chat"}:
                payload: dict[str, Any] = {
                    "ok": False,
                    "error": "unknown method",
                }
            else:
                payload = {"ok": True, "result": self._snapshot(method)}
            writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
