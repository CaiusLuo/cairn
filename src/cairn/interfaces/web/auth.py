"""Minimal local password auth for the public web entry.

The web process remains loopback-only. This module adds an application-level
session cookie so an operator-facing nginx entry does not expose a browser
Basic-auth prompt or an unauthenticated write path.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "cairn_web_session"
PUBLIC_PATHS = frozenset({"/favicon.svg", "/login.html", "/api/login", "/api/health"})
PUBLIC_PREFIXES = ("/assets/",)
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_SESSIONS = 64


class AuthSessions:
    """Opaque sessions stored as hashes so a service restart keeps users signed in."""

    def __init__(
        self,
        username: str,
        auth_passphrase: str,
        *,
        path: Path | None = None,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self.username = username
        self._auth_passphrase = auth_passphrase
        self._ttl = ttl_seconds
        self._path = Path(path) if path is not None else None
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, dict):
            return
        now = time.time()
        self._tokens = {
            key: float(expiry)
            for key, expiry in sessions.items()
            if isinstance(key, str)
            and isinstance(expiry, (int, float))
            and expiry > now
        }

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=self._path.name + ".", dir=self._path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "sessions": self._tokens}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        except OSError:
            # The in-process session still works if the state file cannot be written.
            pass

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune_locked(self, now: float) -> None:
        for key in [key for key, expiry in self._tokens.items() if expiry <= now]:
            self._tokens.pop(key, None)
        if len(self._tokens) >= MAX_SESSIONS:
            oldest = sorted(self._tokens.items(), key=lambda item: item[1])
            for key, _ in oldest[: len(self._tokens) - MAX_SESSIONS + 1]:
                self._tokens.pop(key, None)

    def authenticate(self, username: str, auth_passphrase: str) -> bool:
        return hmac.compare_digest(username, self.username) and hmac.compare_digest(
            auth_passphrase, self._auth_passphrase
        )

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._tokens[self._key(token)] = now + self._ttl
            self._save_locked()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        with self._lock:
            expiry = self._tokens.get(key)
            if expiry is None:
                return False
            if expiry <= time.time():
                self._tokens.pop(key, None)
                self._save_locked()
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._tokens.pop(self._key(token), None) is not None:
                self._save_locked()


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


def operator_request(request: Request, runtime_token: str | None) -> bool:
    """Accept a host bearer or an authenticated browser on this exact origin."""
    bearer = request.headers.get("Authorization", "")
    return (
        runtime_token is not None
        and hmac.compare_digest(bearer, "Bearer " + runtime_token)
    ) or request.headers.get("Origin") == str(request.base_url).rstrip("/")


def login_redirect_target(scope: Scope) -> str:
    """Return a same-origin page for an already signed-in visitor of /login.html."""
    query: dict[str, list[str]] = parse_qs(
        scope.get("query_string", b"").decode("latin-1")
    )
    target = (query.get("next") or [""])[0]
    if (
        target.startswith("/")
        and not target.startswith("//")
        and not target.startswith("/login.html")
        and "\\" not in target
        and all(0x21 <= ord(character) <= 0x7E for character in target)
    ):
        return target
    return "/"


async def _send_redirect(send: Send, location: str) -> None:
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
    await send({"type": "http.response.body", "body": b""})


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
        if (
            scope["type"] == "http"
            and path == "/login.html"
            and scope.get("method", "GET") in ("GET", "HEAD")
            and self.sessions.valid(cookie_token(scope))
        ):
            await _send_redirect(send, login_redirect_target(scope))
            return
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
        query = scope.get("query_string", b"").decode("ascii", errors="replace")
        if path not in ("", "/") or query:
            target = path + ("?" + query if query else "")
            location += "?" + urlencode({"next": target})
        await _send_redirect(send, location)
