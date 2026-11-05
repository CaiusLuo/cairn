"""Minimal local password auth for the public web entry.

The web process remains loopback-only. This module adds an application-level
session cookie so an operator-facing nginx entry does not expose a browser
Basic-auth prompt or an unauthenticated write path.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "cairn_web_session"
PUBLIC_PATHS = frozenset({"/login.html", "/api/login", "/api/health"})
PUBLIC_PREFIXES = ("/assets/",)


class AuthSessions:
    """In-memory opaque sessions; a process restart intentionally logs everyone out."""

    def __init__(
        self, username: str, auth_passphrase: str, ttl_seconds: int = 12 * 60 * 60
    ) -> None:
        self.username = username
        self._auth_passphrase = auth_passphrase
        self._ttl = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def authenticate(self, username: str, auth_passphrase: str) -> bool:
        return hmac.compare_digest(username, self.username) and hmac.compare_digest(
            auth_passphrase, self._auth_passphrase
        )

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.monotonic() + self._ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry <= time.monotonic():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._tokens.pop(token, None)


def cookie_token(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            cookies = SimpleCookie()
            try:
                cookies.load(value.decode("latin-1"))
            except (ValueError, UnicodeDecodeError):
                return None
            morsel = cookies.get(COOKIE_NAME)
            return morsel.value if morsel is not None else None
    return None


class AuthGuard:
    """Protect pages, API reads and WebSocket upgrades with one cookie session."""

    def __init__(
        self, app: ASGIApp, sessions: AuthSessions, bearer_token: str | None = None
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.bearer_token = bearer_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in PUBLIC_PATHS or any(
            path.startswith(prefix) for prefix in PUBLIC_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        authorization = next(
            (
                value
                for key, value in scope.get("headers", [])
                if key.lower() == b"authorization"
            ),
            b"",
        )
        bearer_valid = self.bearer_token is not None and secrets.compare_digest(
            authorization, ("Bearer " + self.bearer_token).encode("utf-8")
        )
        if bearer_valid or self.sessions.valid(cookie_token(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if path.startswith("/api/"):
            body = b'{"error":"authentication_required"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        location = "/login.html"
        if path not in ("", "/"):
            location += f"?next={path}"
        body = b""
        await send(
            {
                "type": "http.response.start",
                "status": 307,
                "headers": [
                    (b"location", location.encode("ascii")),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
