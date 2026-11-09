"""Authenticated control-plane transport shared by browser and terminal clients."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from cairn.interfaces.web.runtime import request_body


class Controls(Protocol):
    workspace: Any

    async def command(self, request: dict[str, Any]) -> dict[str, Any]: ...
    async def snapshot(self, section: str) -> dict[str, Any]: ...
    def commands(self) -> list[dict[str, Any]]: ...


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    command: str = Field(min_length=1, max_length=40)
    args: str = Field(default="", max_length=32000)
    task_id: str | None = Field(default=None, max_length=100)
    request_id: str = Field(min_length=1, max_length=200)
    source: Literal["cli", "web", "wechat"] = "web"
    confirmed: bool = False
    expected_proposal: str | None = Field(default=None, max_length=64)
    sender: str | None = Field(default=None, max_length=256)
    account: str | None = Field(default=None, max_length=256)


def management_routes(controls: Controls, token: str) -> list[Route]:
    async def handle(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        expected = request.headers.get("X-Cairn-Workspace")
        if expected is not None and expected != controls.workspace.as_uri():
            return JSONResponse({"error": "workspace_mismatch"}, 409, headers=headers)
        try:
            if request.method == "GET":
                result: Any = (
                    controls.commands()
                    if request.url.path.endswith("commands")
                    else await controls.snapshot(
                        request.query_params.get("section", "summary")
                    )
                )
            else:
                bearer = request.headers.get("Authorization", "")
                host_token = hmac.compare_digest(bearer, "Bearer " + token)
                if not host_token and request.headers.get("Origin") != str(
                    request.base_url
                ).rstrip("/"):
                    # Host management needs an exact browser Origin, not merely a
                    # loopback tab on another port with the same cookie.
                    return JSONResponse(
                        {"error": "invalid_origin"}, 403, headers=headers
                    )
                body = CommandRequest.model_validate(await request_body(request))
                if (
                    body.source == "wechat"
                    and body.task_id is not None
                    and not host_token
                ):
                    return JSONResponse(
                        {"error": "runtime_token_required"}, 403, headers=headers
                    )
                result = await controls.command(body.model_dump())
            return JSONResponse(result, headers=headers)
        except (ValueError, KeyError):
            return JSONResponse(
                {"error": "invalid_or_stale_command"}, 409, headers=headers
            )
        except asyncio.QueueFull:
            return JSONResponse({"error": "runtime_queue_full"}, 429, headers=headers)
        except RuntimeError as error:
            return JSONResponse(
                {"error": "operation_uncertain", "message": str(error)},
                409,
                headers=headers,
            )

    async def home(_request: Request) -> RedirectResponse:
        return RedirectResponse("/chat.html", status_code=307)

    return [
        Route("/", home, methods=["GET"]),
        Route("/api/runtime/management", handle, methods=["GET"]),
        Route("/api/runtime/commands", handle, methods=["GET", "POST"]),
    ]
