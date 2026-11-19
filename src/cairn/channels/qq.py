"""QQ Open Platform Gateway and passive text replies for C2C messages."""

from __future__ import annotations

import asyncio
import re
import sys

import httpx2 as httpx

from cairn.channels.adapter import AdapterFactory, InboundSink
from cairn.channels.gateway import (
    GatewayAdapter,
    GatewayOpen,
    GatewaySession,
    gateway_url,
    open_gateway,
)
from cairn.channels.models import (
    ChannelAdapterError,
    ChannelConnection,
    ChannelCredentialError,
    ChannelInboundEvent,
    ChannelPlatform,
    ChannelSendResult,
    ChannelSendStatus,
)
from cairn.channels.store import ChannelStore

_TOKEN_ENDPOINT = "https://bots.qq.com/app/getAppAccessToken"
_API = "https://api.sgroup.qq.com"
_OPENID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_REJECTION_CODES = {400, 401, 403, 404, 413, 422, 429}
_MAX_TEXT_BYTES = 2000


def _object(response: httpx.Response) -> dict[str, object]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class QQAdapter(GatewayAdapter):
    platform = ChannelPlatform.QQ
    fatal_close_codes = GatewayAdapter.fatal_close_codes | {4914, 4915}

    def __init__(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
        *,
        store: ChannelStore,
        socket: GatewayOpen = open_gateway,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(connection, inbound, socket=socket, transport=transport)
        self._app_id = (credentials or {}).get("app_id", "").strip()
        self._secret = (credentials or {}).get("client_secret", "")
        self._store = store
        self._token = ""
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        async with self._token_lock:
            clock = asyncio.get_running_loop().time
            now = clock()
            if self._token and now < self._refresh_at:
                return self._token
            if (
                not re.fullmatch(r"[0-9]{1,32}", self._app_id)
                or not self._secret.strip()
            ):
                raise ChannelCredentialError("qq_credentials_invalid")
            response = await http.post(
                _TOKEN_ENDPOINT,
                json={"appId": self._app_id, "clientSecret": self._secret},
            )
            data = _object(response)
            token, ttl = data.get("access_token"), data.get("expires_in")
            if response.status_code in (400, 401, 403):
                raise ChannelCredentialError("qq_credentials_rejected")
            response.raise_for_status()
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[!-~]+", token)
                or not isinstance(ttl, (int, str))
                or not re.fullmatch(r"[0-9]{1,8}", str(ttl))
                or int(ttl) <= 0
            ):
                raise ChannelCredentialError("qq_token_unavailable")
            self._token = token
            self._refresh_at = now + int(ttl) - min(60, int(ttl) / 2)
            return self._token

    def _on_close(self, code: int | None) -> None:
        if code in (4004, 4006, 4007, 4009) or (
            code is not None and 4900 <= code <= 4913
        ):
            self._session = None
            self._refresh_at = 0

    async def _handshake(
        self,
        session: GatewaySession | None,
    ) -> tuple[str, dict[str, object]]:
        http = self._http
        assert http is not None
        token = "QQBot " + await self._access_token(http)
        response = await http.get(f"{_API}/gateway", headers={"Authorization": token})
        if response.status_code in (401, 403):
            self._refresh_at = 0
            raise ChannelCredentialError("qq_gateway_unauthorized")
        response.raise_for_status()
        url = gateway_url(_object(response).get("url"), ("sgroup.qq.com", "bot.qq.com"))
        if session is not None:
            return url, {
                "op": 6,
                "d": {
                    "token": token,
                    "session_id": session.session_id,
                    "seq": session.sequence,
                },
            }
        return url, {
            "op": 2,
            "d": {
                "token": token,
                "intents": 1 << 25,
                "shard": [0, 1],
                "properties": {
                    "$os": sys.platform,
                    "$browser": "cairn",
                    "$device": "cairn",
                },
            },
        }

    def _ready(self, data: dict[str, object], seq: int) -> GatewaySession:
        user, session_id = data.get("user"), data.get("session_id")
        bot_id = user.get("id") if isinstance(user, dict) else None
        if (
            not isinstance(bot_id, str)
            or not _OPENID.fullmatch(bot_id)
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise ChannelAdapterError("qq_identity_unavailable")
        return GatewaySession(session_id, bot_id, seq, None)

    def _message(
        self,
        kind: str,
        data: dict[str, object],
        bot_id: str,
    ) -> ChannelInboundEvent | None:
        author = data.get("author")
        if kind != "C2C_MESSAGE_CREATE" or not isinstance(author, dict):
            return None
        sender = author.get("user_openid")
        text, msg_id = data.get("content"), data.get("id")
        if (
            author.get("bot")
            or sender == bot_id
            or not isinstance(sender, str)
            or not _OPENID.fullmatch(sender)
            or not isinstance(msg_id, str)
            or not msg_id.strip()
            or len(msg_id) > 200
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 32000
        ):
            return None
        return ChannelInboundEvent(
            platform=self.platform,
            connection_id=self.connection_id,
            adapter_identity=bot_id,
            sender_id=sender,
            conversation_binding=sender,
            external_message_id=msg_id,
            text=text,
        )

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        bot = self.identity()
        http = self._http
        if (
            not bot
            or http is None
            or not _OPENID.fullmatch(target)
            or not message.strip()
        ):
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="qq_send_invalid"
            )
        encoded = message.encode("utf-8")
        if len(encoded) > _MAX_TEXT_BYTES:
            note = "\n[回复过长, 完整结果请在 Cairn Web 查看]"
            message = (
                encoded[: _MAX_TEXT_BYTES - len(note.encode("utf-8"))].decode(
                    "utf-8", errors="ignore"
                )
                + note
            )
        payload: dict[str, object] = {"msg_type": 0, "content": message}
        if idempotency_key.startswith("channel-reply:"):
            reply = self._store.reply_for_run(
                idempotency_key.removeprefix("channel-reply:")
            )
            if (
                reply is None
                or reply.connection_id != self.connection_id
                or reply.adapter_identity != bot
                or reply.target != target
                or reply.sender_id != target
                or not reply.external_message_id
            ):
                return ChannelSendResult(
                    status=ChannelSendStatus.REJECTED, error="qq_reply_context_invalid"
                )
            payload.update(msg_id=reply.external_message_id, msg_seq=1)
        try:
            token = await self._access_token(http)
        except (ChannelAdapterError, httpx.HTTPError):
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="qq_token_unavailable"
            )
        if self.identity() != bot:
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="qq_session_lost"
            )
        try:
            response = await http.post(
                f"{_API}/v2/users/{target}/messages",
                json=payload,
                headers={"Authorization": f"QQBot {token}"},
            )
        except httpx.HTTPError:
            return ChannelSendResult(
                status=ChannelSendStatus.UNKNOWN, error="qq_send_unconfirmed"
            )
        data = _object(response)
        message_id = data.get("id")
        if (
            response.is_success
            and data.get("code", 0) == 0
            and isinstance(message_id, str)
            and message_id.strip()
        ):
            return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)
        if response.status_code == 401:
            self._refresh_at = 0
        if data.get("code") != 40054005 and response.status_code in _REJECTION_CODES:
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="qq_send_rejected"
            )
        return ChannelSendResult(
            status=ChannelSendStatus.UNKNOWN, error="qq_send_unconfirmed"
        )


def qq_adapter_factory(store: ChannelStore) -> AdapterFactory:
    def build(
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
    ) -> QQAdapter:
        return QQAdapter(connection, credentials, inbound, store=store)

    return build
