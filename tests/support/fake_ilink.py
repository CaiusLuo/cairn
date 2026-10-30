"""Scriptable local iLink HTTP server for offline tests (no real WeChat)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class FakeILink:
    def __init__(self) -> None:
        self.qr_statuses: list[dict[str, Any]] = []
        self.confirm: dict[str, Any] = {}
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.verify_codes: list[str] = []
        self.config_calls: list[dict[str, Any]] = []
        self.typing_calls: list[dict[str, Any]] = []
        self.qr_posts = 0
        self.last_cursor = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def handle(
        self, method: str, path: str, headers: Any, body: bytes
    ) -> dict[str, Any]:
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                payload = {}
        self.requests.append(
            {"method": method, "path": path, "headers": dict(headers), "body": payload}
        )
        route = parsed.path
        if route == "/ilink/bot/get_bot_qrcode" and method == "POST":
            self.qr_posts += 1
            if query.get("bot_type") != ["3"]:
                return {"error": "missing bot_type=3"}
            return {
                "qrcode": "Q-CODE",
                "qrcode_img_content": "https://example.invalid/qr",
            }
        if route == "/ilink/bot/get_qrcode_status" and method == "GET":
            if query.get("verify_code"):
                self.verify_codes.append(query["verify_code"][0])
            status = self.qr_statuses.pop(0) if self.qr_statuses else {"status": "wait"}
            if status.get("status") == "confirmed":
                return {**status, **self.confirm}
            return status
        if route == "/ilink/bot/getupdates" and method == "POST":
            self.last_cursor = str(payload.get("get_updates_buf", ""))
            if self.updates:
                return self.updates.pop(0)
            return {"ret": 0, "msgs": [], "get_updates_buf": self.last_cursor}
        if route == "/ilink/bot/getconfig" and method == "POST":
            self.config_calls.append(payload)
            return {"ret": 0, "typing_ticket": "typing-ticket-1"}
        if route == "/ilink/bot/sendtyping" and method == "POST":
            self.typing_calls.append(payload)
            return {"ret": 0}
        if route == "/ilink/bot/sendmessage" and method == "POST":
            msg = payload.get("msg")
            if isinstance(msg, dict):
                self.sent.append(msg)
            return {"ret": 0}
        if route in ("/ilink/bot/msg/notifystart", "/ilink/bot/msg/notifystop"):
            return {"ret": 0}
        return {"error": "not found"}

    def start(self) -> "FakeILink":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                result = fake.handle(self.command, self.path, self.headers, body)
                data = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                self._do()

            def do_GET(self) -> None:
                self._do()

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "FakeILink":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
