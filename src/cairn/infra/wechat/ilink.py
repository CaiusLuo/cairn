"""HTTP JSON client for the WeChat iLink bot protocol.

This implements the documented login, getUpdates and sendMessage calls. It does
not create an Agent turn and never logs the bot token. The caller owns retry and
task policy; this module maps HTTP/business failures to ILinkError.
"""

from __future__ import annotations

import base64
import random
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "0.1.0"
APP_ID = "bot"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ILinkError(RuntimeError):
    """A protocol or transport failure. Messages never include the token."""


def validate_base_url(url: str, *, allow_insecure_local: bool = False) -> str:
    """Reject plaintext/credentialed targets before a token is sent to them.

    Production base URLs must be https with no userinfo. ``allow_insecure_local``
    is an explicit test-only escape hatch for a loopback fake server.
    """
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ILinkError("base URL must not contain userinfo")
    if parsed.scheme == "https" and parsed.hostname:
        if parsed.path not in ("", "/"):
            raise ILinkError("base URL must be an origin without a path")
        return url.rstrip("/")
    if (
        allow_insecure_local
        and parsed.scheme == "http"
        and parsed.hostname in _LOCAL_HOSTS
        and parsed.path in ("", "/")
    ):
        return url.rstrip("/")
    raise ILinkError("base URL must be an https origin")


@dataclass(frozen=True, slots=True)
class QRCode:
    qrcode: str
    image_content: str


def redirect_base_url(host: str) -> str:
    """Build and validate an HTTPS base URL from a redirect host.

    A bare host or host:port is promoted to ``https://``; any explicit scheme
    must already be https, and credentials/paths are rejected. The caller never
    treats the raw host as a usable base URL.
    """
    candidate = host.strip()
    if not candidate:
        raise ILinkError("empty redirect host")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ILinkError("redirect endpoint must be an https URL")
    if parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise ILinkError("redirect endpoint must be a bare https origin")
    return f"https://{parsed.netloc}"


class ILinkClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        bot_token: str | None = None,
        channel_version: str = CHANNEL_VERSION,
        timeout: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_insecure_local: bool = False,
    ) -> None:
        self.base_url = validate_base_url(
            base_url, allow_insecure_local=allow_insecure_local
        )
        self.bot_token = bot_token
        self.channel_version = channel_version
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )
        self._uin = base64.b64encode(
            str(random.SystemRandom().randint(0, 2**32 - 1)).encode("ascii")
        ).decode("ascii")
        self._client_version = str(
            (0 << 24) | (1 << 16) | (0 << 8) | 0
        )  # 0x00MMNNPP for 0.1.0

    def _app_headers(self) -> dict[str, str]:
        return {
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": self._client_version,
        }

    def _json_headers(self, *, auth: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._uin,
            **self._app_headers(),
        }
        if auth:
            if not self.bot_token:
                raise ILinkError("bot token is required for this request")
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        auth: bool = True,
        base_info: bool = True,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = dict(payload)
        if base_info:
            body["base_info"] = {
                "channel_version": self.channel_version,
                "bot_agent": "Cairn",
            }
        try:
            response = await self._client.post(
                path,
                json=body,
                headers=self._json_headers(auth=auth),
                params=params,
            )
        except httpx.HTTPError as exc:
            raise ILinkError(f"HTTP request failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise ILinkError(f"HTTP {response.status_code} from iLink")
        try:
            data = response.json()
        except ValueError:
            raise ILinkError("iLink returned non-JSON") from None
        if not isinstance(data, dict):
            raise ILinkError("iLink returned a non-object response")
        return data

    async def get_qrcode(self, local_token_list: list[str] | None = None) -> QRCode:
        data = await self._post(
            "/ilink/bot/get_bot_qrcode",
            {"local_token_list": list(local_token_list or [])},
            auth=False,
            base_info=False,
            params={"bot_type": "3"},
        )
        qrcode = data.get("qrcode")
        image = data.get("qrcode_img_content")
        if not isinstance(qrcode, str) or not qrcode:
            raise ILinkError("iLink did not return a qrcode")
        return QRCode(qrcode=qrcode, image_content=str(image or ""))

    async def get_qrcode_status(
        self, qrcode: str, verify_code: str | None = None
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            response = await self._client.get(
                "/ilink/bot/get_qrcode_status",
                params=params,
                headers=self._app_headers(),
            )
        except httpx.HTTPError as exc:
            raise ILinkError(f"HTTP request failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise ILinkError(f"HTTP {response.status_code} from iLink")
        try:
            data = response.json()
        except ValueError:
            raise ILinkError("iLink returned non-JSON") from None
        if not isinstance(data, dict):
            raise ILinkError("iLink returned a non-object response")
        return data

    async def get_updates(
        self, cursor: str, *, longpoll_timeout_ms: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"get_updates_buf": cursor}
        if longpoll_timeout_ms is not None:
            payload["longpolling_timeout_ms"] = longpoll_timeout_ms
        return await self._post("/ilink/bot/getupdates", payload)

    async def get_config(
        self, *, ilink_user_id: str, context_token: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"ilink_user_id": ilink_user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self._post("/ilink/bot/getconfig", payload)

    async def send_typing(
        self, *, ilink_user_id: str, typing_ticket: str, status: int
    ) -> None:
        data = await self._post(
            "/ilink/bot/sendtyping",
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
        )
        ret = data.get("ret")
        if ret not in (None, 0):
            raise ILinkError(f"sendTyping failed with ret={ret}")

    async def send_text(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        data = await self._post("/ilink/bot/sendmessage", {"msg": msg})
        ret = data.get("ret")
        if ret not in (None, 0):
            raise ILinkError(f"sendMessage failed with ret={ret}")
        return data

    async def notify_start(self) -> None:
        await self._post("/ilink/bot/msg/notifystart", {})

    async def notify_stop(self) -> None:
        await self._post("/ilink/bot/msg/notifystop", {})

    async def close(self) -> None:
        await self._client.aclose()
