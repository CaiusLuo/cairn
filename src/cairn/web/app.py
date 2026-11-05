"""Thin ASGI surface over the read-only SnapshotReader.

The HTTP surface exposes only GET snapshots and committed Session history. The
WebSocket surface emits refresh signals; it accepts no commands and advances no
Task lifecycle. Host and Origin are checked before either protocol reaches a
reader.
"""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from cairn.core.task_board import TaskBoardStatus
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.task_board.store import (
    TaskBoardBusy,
    TaskBoardRevisionConflict,
    TaskBoardStore,
    TaskBoardValidationError,
)
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService
from cairn.web.auth import COOKIE_NAME, AuthGuard, AuthSessions
from cairn.web.config import WebConfig
from cairn.web.reader import SnapshotReader

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LoopbackGuard:
    """Reject non-loopback Host and cross-origin browser requests.

    The adapter is intentionally loopback-only. A missing Origin is accepted for
    local CLI probes; a present Origin must name a loopback host.
    """

    def __init__(self, app: ASGIApp, config: WebConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if not self._host_allowed(host):
            await self._reject(scope, send, 400, "invalid_host")
            return
        origin = headers.get("origin")
        if origin is not None and not self._origin_allowed(origin):
            await self._reject(scope, send, 403, "invalid_origin")
            return
        await self.app(scope, receive, send)

    def _host_allowed(self, host: str) -> bool:
        if not host:
            return False
        if host.startswith("["):
            name, _, port = host[1:].partition("]")
            name = name.lower()
        else:
            name, _, port = host.partition(":")
            name = name.lower()
        if name not in _ALLOWED_HOSTS:
            return False
        if not port:
            return True
        return port == str(self.config.port)

    def _origin_allowed(self, origin: str) -> bool:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https") or parsed.hostname is None:
            return False
        # Any loopback dev origin is local; the bind host remains loopback and
        # the HTTP Host guard still pins the serving port below.
        return (
            parsed.hostname in _ALLOWED_HOSTS
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    async def _reject(scope: Scope, send: Send, status: int, error: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = f'{{"error":"{error}"}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _json_headers(
    content_type: str = "application/json; charset=utf-8",
) -> dict[str, str]:
    return {"content-type": content_type, "cache-control": "no-store"}


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_json_headers())


def create_app(
    config: WebConfig,
    reader: SnapshotReader | None = None,
    *,
    runtime: TaskService | None = None,
    runtime_token: str | None = None,
) -> Starlette:
    if runtime is not None and (not config.auth_passphrase or not runtime_token):
        raise ValueError("Runtime routes require authenticated host configuration")
    reader = reader or SnapshotReader(
        workspace=config.workspace,
        sessions_dir=config.sessions_dir,
        tasks_dir=config.tasks_dir,
    )

    task_service = TaskBoardService(TaskBoardStore(config.tasks_dir))

    auth_sessions = AuthSessions(config.username, config.auth_passphrase)

    async def login_page(_request: Request) -> Response:
        return RedirectResponse("/login.html", status_code=307)

    async def login(request: Request) -> Response:
        if not config.auth_passphrase:
            return _error(404, "login_disabled")
        try:
            payload = await request.json()
        except ValueError:
            return _error(400, "invalid_json")
        if (
            not isinstance(payload, dict)
            or set(payload) != {"username", "auth_passphrase"}
            or not isinstance(payload["username"], str)
            or not isinstance(payload["auth_passphrase"], str)
        ):
            return _error(422, "invalid_credentials")
        if not auth_sessions.authenticate(
            payload["username"], payload["auth_passphrase"]
        ):
            return _error(401, "invalid_credentials")
        token = auth_sessions.issue()
        response = JSONResponse({"ok": True}, headers=_json_headers())
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"ok": True, "version": 1, "read_only": runtime is None},
            headers=_json_headers(),
        )

    async def snapshot(_request: Request) -> JSONResponse:
        try:
            data = await asyncio.to_thread(reader.snapshot)
        except (OSError, ValueError):
            return JSONResponse(
                {"error": "unavailable"}, status_code=503, headers=_json_headers()
            )
        return JSONResponse(data, headers=_json_headers())

    async def session_detail(request: Request) -> Response:
        session_id = request.path_params.get("session_id", "")
        try:
            data = await asyncio.to_thread(reader.session_detail, session_id)
        except (SessionStoreError, OSError, ValueError):
            data = None
        if data is None:
            return JSONResponse(
                {"error": "session_not_found"},
                status_code=404,
                headers=_json_headers(),
            )
        return JSONResponse(data, headers=_json_headers())

    async def update_task(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = await request.json()
        except ValueError:
            return _error(400, "invalid_json")
        if not isinstance(payload, dict) or set(payload) != {
            "status",
            "expected_revision",
        }:
            return _error(422, "invalid_task_update")
        status_value = payload["status"]
        expected_revision = payload["expected_revision"]
        if not isinstance(status_value, str) or type(expected_revision) is not int:
            return _error(422, "invalid_task_update")
        try:
            status = TaskBoardStatus(status_value)
        except ValueError:
            return _error(422, "invalid_status")
        task_id = request.path_params.get("task_id", "")
        try:
            task = await task_service.amove_status(task_id, expected_revision, status)
        except TaskBoardRevisionConflict as conflict:
            current = await task_service.aget(task_id)
            body: dict[str, object] = {
                "error": "revision_conflict",
                "expected_revision": expected_revision,
                "current_revision": conflict.actual,
            }
            if current is not None:
                body["task"] = reader.task_view(current)
            return JSONResponse(body, status_code=409, headers=_json_headers())
        except TaskBoardBusy:
            return _error(503, "task_board_busy")
        except TaskBoardValidationError as exc:
            return JSONResponse(
                {"error": "invalid_task_update", "detail": str(exc)},
                status_code=422,
                headers=_json_headers(),
            )
        except (OSError, ValueError):
            return _error(503, "task_board_unavailable")
        if task is None:
            return _error(404, "task_not_found")
        fresh = await asyncio.to_thread(reader.snapshot)
        return JSONResponse(
            {
                "task": reader.task_view(task),
                "sequence": fresh["sequence"],
                "generated_at_utc": fresh["generated_at_utc"],
                "execution_unchanged": True,
            },
            headers=_json_headers(),
        )

    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        last_sequence: int | None = None
        try:
            initial = await asyncio.to_thread(reader.snapshot)
            last_sequence = int(initial["sequence"])
            await websocket.send_json(
                {
                    "type": "ready",
                    "version": initial["version"],
                    "sequence": initial["sequence"],
                    "generated_at_utc": initial["generated_at_utc"],
                    "read_only": True,
                }
            )
            while True:
                try:
                    await asyncio.wait_for(
                        websocket.receive_text(), timeout=config.refresh_seconds
                    )
                except TimeoutError:
                    current = await asyncio.to_thread(reader.snapshot)
                    if int(current["sequence"]) != last_sequence:
                        last_sequence = int(current["sequence"])
                        await websocket.send_json(
                            {
                                "type": "refresh",
                                "version": current["version"],
                                "sequence": current["sequence"],
                                "generated_at_utc": current["generated_at_utc"],
                            }
                        )
        except WebSocketDisconnect:
            return
        except (OSError, ValueError, SessionStoreError):
            return
        finally:
            # No background task is retained here. Starlette closes the socket on
            # context exit; an explicit close is harmless if it already closed.
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/login", login, methods=["POST"]),
        Route("/login", login_page, methods=["GET"]),
        Route("/api/snapshot", snapshot, methods=["GET"]),
        Route("/api/sessions/{session_id}", session_detail, methods=["GET"]),
        Route("/api/tasks/{task_id}", update_task, methods=["PATCH"]),
        WebSocketRoute("/api/ws", websocket_status),
    ]
    if runtime is not None:
        from cairn.web.runtime import task_routes

        routes.extend(task_routes(runtime))
    web_root = config.web_root
    if web_root is not None and (web_root / "index.html").is_file():
        routes.append(
            Mount("/", app=StaticFiles(directory=web_root, html=True), name="web")
        )
    middleware = [Middleware(LoopbackGuard, config=config)]
    if config.auth_passphrase:
        middleware.append(
            Middleware(AuthGuard, sessions=auth_sessions, bearer_token=runtime_token)
        )
    return Starlette(routes=routes, middleware=middleware)
