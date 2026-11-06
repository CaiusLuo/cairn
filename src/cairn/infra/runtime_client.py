"""Loopback HTTP client shared by command-line and messaging adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


class RuntimeClient:
    def __init__(
        self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Runtime URL must be a loopback HTTP origin")
        if not token.strip():
            raise ValueError("Runtime token is required")
        self.url = url.rstrip("/")
        self.token = token
        self.transport = transport

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        async with httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": "Bearer " + self.token},
            timeout=20,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method, "/api/runtime/" + path, json=payload
            )
            response.raise_for_status()
            return response.json()

    async def submit(
        self, prompt: str, source: str, request_id: str | None = None
    ) -> dict[str, Any]:
        return dict(
            await self.request(
                "POST",
                "tasks",
                {"prompt": prompt, "source": source, "request_id": request_id},
            )
        )

    async def message(
        self,
        task_id: str,
        prompt: str,
        *,
        source: str,
        request_id: str,
        expected_sequence: int,
    ) -> dict[str, Any]:
        self._id(task_id)
        return dict(
            await self.request(
                "POST",
                f"tasks/{task_id}/messages",
                {
                    "prompt": prompt,
                    "source": source,
                    "request_id": request_id,
                    "expected_sequence": expected_sequence,
                },
            )
        )

    async def inspect(self, task_id: str) -> dict[str, Any]:
        self._id(task_id)
        return dict(await self.request("GET", "tasks/" + task_id))

    async def control(self, task_id: str, action: str) -> dict[str, Any]:
        self._id(task_id)
        if action not in {"continue", "cancel"}:
            raise ValueError("Invalid runtime action")
        return dict(await self.request("POST", f"tasks/{task_id}/{action}"))

    @staticmethod
    def _id(task_id: str) -> None:
        if not task_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for c in task_id
        ):
            raise ValueError("Invalid task id")


def read_runtime_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Runtime token must be a private regular host file")
    token = path.read_text().strip()
    if not token:
        raise ValueError("Runtime token is empty")
    return token
