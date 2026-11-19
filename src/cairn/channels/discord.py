"""Discord Gateway v10 and at-most-once text delivery for bot DMs."""

from __future__ import annotations

import hashlib
import json
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

_GATEWAY = "wss://gateway.discord.gg"
_API = "https://discord.com/api/v10"
_REJECTION_CODES = {400, 401, 403, 404, 413, 422, 429}


def _snowflake(value: object) -> str | None:
    if (
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{0,19}", value)
        and int(value) < 1 << 64
    ):
        return value
    return None


class DiscordAdapter(GatewayAdapter):
    platform = ChannelPlatform.DISCORD
    fatal_close_codes = GatewayAdapter.fatal_close_codes | {4004}

    def __init__(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
        *,
        socket: GatewayOpen = open_gateway,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(connection, inbound, socket=socket, transport=transport)
        self._token = (credentials or {}).get("bot_token", "").strip()

    async def _handshake(
        self,
        session: GatewaySession | None,
    ) -> tuple[str, dict[str, object]]:
        if not self._token or any(c.isspace() for c in self._token):
            raise ChannelCredentialError("discord_bot_token_invalid")
        if session is not None and session.resume_url is not None:
            return session.resume_url, {
                "op": 6,
                "d": {
                    "token": self._token,
                    "session_id": session.session_id,
                    "seq": session.sequence,
                },
            }
        # Message Content is available in DMs with the app without the privileged intent.
        return _GATEWAY + "/?v=10&encoding=json", {
            "op": 2,
            "d": {
                "token": self._token,
                "intents": 1 << 12,
                "properties": {
                    "os": sys.platform,
                    "browser": "cairn",
                    "device": "cairn",
                },
            },
        }

    def _ready(self, data: dict[str, object], seq: int) -> GatewaySession:
        user = data.get("user")
        bot_id = (
            _snowflake(user.get("id"))
            if isinstance(user, dict) and user.get("bot") is True
            else None
        )
        session_id = data.get("session_id")
        if bot_id is None or not isinstance(session_id, str) or not session_id:
            raise ChannelAdapterError("discord_identity_unavailable")
        base = gateway_url(data.get("resume_gateway_url"), ("discord.gg",))
        if re.fullmatch(r"wss://[a-zA-Z0-9.-]+\.discord\.gg/?", base) is None:
            raise ChannelAdapterError("discord_resume_url_invalid")
        return GatewaySession(
            session_id, bot_id, seq, base.rstrip("/") + "/?v=10&encoding=json"
        )

    def _message(
        self,
        kind: str,
        data: dict[str, object],
        bot_id: str,
    ) -> ChannelInboundEvent | None:
        if (
            kind != "MESSAGE_CREATE"
            or "guild_id" in data
            or "webhook_id" in data
            or data.get("type") not in (0, 19)
        ):
            return None
        author = data.get("author")
        if not isinstance(author, dict) or author.get("bot") or author.get("system"):
            return None
        sender = _snowflake(author.get("id"))
        channel = _snowflake(data.get("channel_id"))
        message = _snowflake(data.get("id"))
        content = data.get("content")
        if not sender or sender == bot_id or not channel or not message:
            return None
        if not isinstance(content, str) or not content.strip() or len(content) > 32000:
            return None
        return ChannelInboundEvent(
            connection_id=self.connection_id,
            platform=self.platform,
            external_message_id=message,
            conversation_binding=channel,
            sender_id=sender,
            adapter_identity=bot_id,
            text=content,
        )

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        bot = self.identity()
        http = self._http
        if not bot or http is None:
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="discord_not_connected"
            )
        if not _snowflake(target) or not message.strip():
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="discord_invalid_message"
            )
        offset = 0
        while offset < len(message):
            chunk = (
                message[offset : offset + 2000]
                .encode("utf-16-le")[:4000]
                .decode(
                    "utf-16-le",
                    errors="ignore",
                )
            )
            if self.identity() != bot:
                return ChannelSendResult(
                    status=ChannelSendStatus.UNKNOWN
                    if offset
                    else ChannelSendStatus.REJECTED,
                    error="discord_session_lost",
                )
            nonce = hashlib.sha256(
                json.dumps(
                    [self.connection_id, target, idempotency_key, offset]
                ).encode()
            ).hexdigest()[:24]
            try:
                response = await http.post(
                    f"{_API}/channels/{target}/messages",
                    headers={"Authorization": f"Bot {self._token}"},
                    json={
                        "content": chunk,
                        "nonce": nonce,
                        "enforce_nonce": True,
                        "allowed_mentions": {"parse": []},
                    },
                )
                if response.status_code in _REJECTION_CODES:
                    return ChannelSendResult(
                        status=ChannelSendStatus.UNKNOWN
                        if offset
                        else ChannelSendStatus.REJECTED,
                        error=f"discord_http_{response.status_code}",
                    )
                data = response.json()
                if response.is_success and isinstance(data, dict):
                    author = data.get("author")
                    if (
                        _snowflake(data.get("id"))
                        and data.get("channel_id") == target
                        and isinstance(author, dict)
                        and author.get("id") == bot
                    ):
                        offset += len(chunk)
                        continue
            except (httpx.HTTPError, ValueError):
                pass
            return ChannelSendResult(
                status=ChannelSendStatus.UNKNOWN, error="discord_send_unconfirmed"
            )
        return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)


def discord_adapter_factory() -> AdapterFactory:
    return DiscordAdapter
