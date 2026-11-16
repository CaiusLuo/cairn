"""Host-owned channel configuration, credentials and runtime facts.

Channel definitions are deliberately small: they describe a connection and the
observable state of its adapter, not a second Agent or execution runtime.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChannelPlatform(StrEnum):
    WECHAT = "wechat"
    QQ = "qq"
    TELEGRAM = "telegram"
    DISCORD = "discord"


ALL_PLATFORMS: tuple[ChannelPlatform, ...] = tuple(ChannelPlatform)


class ChannelDesiredState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class ChannelActualState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    CONNECTING = "connecting"
    ERROR = "error"


class ChannelDisplayState(StrEnum):
    UNCONFIGURED = "unconfigured"
    RUNNING = "running"
    SILENT = "silent"


class ChannelSendStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_connection_id() -> str:
    return "ch-" + uuid4().hex


class ChannelConnection(BaseModel):
    """One configured connection; credentials live outside this model."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    connection_id: str = Field(min_length=1, max_length=100)
    platform: ChannelPlatform
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool
    desired_state: ChannelDesiredState
    credential_ref: str | None = Field(default=None, max_length=100)
    owner_identity: str | None = Field(default=None, max_length=200)
    # Additional platform identities allowed to submit inbound work. The owner
    # is always authorized; an empty set therefore means owner-only.
    allowed_senders: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    # A legacy connection is projected from an existing host-owned service
    # (currently WeChat); it is never persisted by the channel store.
    legacy: bool = False

    @field_validator("allowed_senders")
    @classmethod
    def _clean_allowed_senders(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_allowed_senders(value)


class ChannelRuntimeStatus(BaseModel):
    """Observable adapter state; never inferred from configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    connection_id: str = Field(min_length=1, max_length=100)
    actual_state: ChannelActualState
    last_connected_at: str | None = None
    last_heartbeat_at: str | None = None
    last_error: str | None = None
    next_retry_at: str | None = None


class ChannelAdapterHealth(BaseModel):
    """The adapter's own report; the manager adds retry scheduling."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    actual_state: ChannelActualState
    last_connected_at: str | None = None
    last_heartbeat_at: str | None = None
    last_error: str | None = None


class ChannelInboundEvent(BaseModel):
    """One normalised inbound message from any adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    platform: ChannelPlatform
    connection_id: str = Field(min_length=1, max_length=100)
    external_message_id: str = Field(min_length=1, max_length=200)
    sender_id: str = Field(min_length=1, max_length=200)
    conversation_binding: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=32000)
    received_at: str = Field(default_factory=utc_now)


class ChannelInboundResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    state: str
    duplicate: bool = False
    conversation_id: str | None = None
    run_id: str | None = None
    error: str | None = None


class ChannelSendResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status: ChannelSendStatus
    error: str | None = None


class ChannelPlatformSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    platform: ChannelPlatform
    display_state: ChannelDisplayState
    connections: int = Field(ge=0)
    running: int = Field(ge=0)


class ChannelCredentialField(BaseModel):
    """One write-only credential input the Web form may collect."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    required: bool = True
    secret: bool = True
    placeholder: str | None = Field(default=None, max_length=120)


class ChannelPlatformCapabilities(BaseModel):
    """Server-authored capabilities; the Web console never guesses them."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    platform: ChannelPlatform
    configured: bool
    adapter_supported: bool
    legacy_managed: bool
    credential_schema: tuple[ChannelCredentialField, ...]
    supported_actions: tuple[str, ...]


PLATFORM_CREDENTIAL_SCHEMAS: dict[
    ChannelPlatform, tuple[ChannelCredentialField, ...]
] = {
    ChannelPlatform.WECHAT: (),
    ChannelPlatform.QQ: (
        ChannelCredentialField(
            key="app_id",
            label="App ID",
            secret=False,
            placeholder="QQ 机器人 App ID",
        ),
        ChannelCredentialField(
            key="client_secret",
            label="Client Secret",
            placeholder="仅写入, 不回显",
        ),
    ),
    ChannelPlatform.TELEGRAM: (
        ChannelCredentialField(
            key="bot_token",
            label="Bot Token",
            placeholder="123456:ABC…",
        ),
    ),
    ChannelPlatform.DISCORD: (
        ChannelCredentialField(
            key="bot_token",
            label="Bot Token",
            placeholder="仅写入, 不回显",
        ),
    ),
}


class ChannelError(Exception):
    """Base for channel failures that are safe to present to the host."""


class ChannelNotFound(ChannelError):
    pass


class ChannelRequestConflict(ChannelError):
    pass


class ChannelRevisionConflict(ChannelError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__("channel revision conflict")


class ChannelOperationUncertain(ChannelError):
    pass


class ChannelConfirmationRequired(ChannelError):
    pass


class ChannelInboundConflict(ChannelError):
    pass


class ChannelInboundRejected(ChannelError):
    pass


class ChannelInboundUncertain(ChannelError):
    """The inbound request may have been accepted; reconcile before retrying."""


class ChannelDeliveryUncertain(ChannelError):
    pass


class ChannelDisabled(ChannelError):
    pass


class ChannelLegacyManaged(ChannelError):
    pass


class ChannelUnsupportedPlatform(ChannelError):
    pass


class ChannelAdapterError(ChannelError):
    pass


class ChannelAdapterNotImplemented(ChannelAdapterError):
    """No real adapter is registered for this platform yet."""


class ChannelCredentialError(ChannelAdapterError):
    pass


_CREDENTIAL_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "credential",
    "api_key",
    "apikey",
)
_MAX_CREDENTIALS = 16
_MAX_CREDENTIAL_VALUE = 4096
_MAX_METADATA_BYTES = 8192


def validate_credentials(value: object) -> dict[str, str]:
    """Validate write-only credentials without allowing URL fetches in C0."""
    if not isinstance(value, dict) or not value:
        raise ValueError("credentials must be a non-empty object")
    if len(value) > _MAX_CREDENTIALS:
        raise ValueError("too many credential fields")
    result: dict[str, str] = {}
    total = 0
    for key, item in value.items():
        if not isinstance(key, str) or not _CREDENTIAL_KEY.fullmatch(key):
            raise ValueError("invalid credential field name")
        if not isinstance(item, str) or not item.strip():
            raise ValueError("credential values must be non-empty strings")
        if len(item) > _MAX_CREDENTIAL_VALUE:
            raise ValueError("credential value is too long")
        lowered = key.lower()
        if any(marker in lowered for marker in ("url", "uri", "webhook", "endpoint")):
            raise ValueError("credential URLs are not accepted in C0")
        if "://" in item or item.lower().startswith(("http:", "https:", "ws:", "wss:")):
            raise ValueError("credential URLs are not accepted in C0")
        total += len(key) + len(item)
        result[key] = item
    if total > 16 * 1024:
        raise ValueError("credentials are too large")
    return result


def validate_allowed_senders(value: object) -> tuple[str, ...]:
    """Normalize an explicit sender allowlist; empty means owner-only."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowed_senders must be a list of strings")
    if len(value) > 32:
        raise ValueError("allowed_senders has too many entries")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("allowed_senders must be strings")
        sender = item.strip()
        if not sender or len(sender) > 200:
            raise ValueError("allowed_senders entries must be 1-200 characters")
        if sender not in result:
            result.append(sender)
    return tuple(result)


def validate_metadata(value: object) -> dict[str, Any]:
    """Bound host-visible metadata and keep obvious secrets out of it."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    if len(value) > 32:
        raise ValueError("metadata has too many fields")

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 64:
                    raise ValueError("invalid metadata key")
                lowered = key.lower()
                if any(marker in lowered for marker in _SECRET_MARKERS):
                    raise ValueError("metadata must not contain credentials")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            if isinstance(item, str) and len(item) > 1024:
                raise ValueError("metadata value is too long")
        else:
            raise ValueError("metadata must be JSON-compatible")

    walk(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ValueError("metadata must be JSON-compatible") from None
    if len(encoded.encode()) > _MAX_METADATA_BYTES:
        raise ValueError("metadata is too large")
    return value


__all__ = [
    "ALL_PLATFORMS",
    "PLATFORM_CREDENTIAL_SCHEMAS",
    "ChannelActualState",
    "ChannelAdapterError",
    "ChannelAdapterHealth",
    "ChannelAdapterNotImplemented",
    "ChannelConfirmationRequired",
    "ChannelConnection",
    "ChannelCredentialError",
    "ChannelCredentialField",
    "ChannelDeliveryUncertain",
    "ChannelDesiredState",
    "ChannelDisabled",
    "ChannelDisplayState",
    "ChannelError",
    "ChannelInboundConflict",
    "ChannelInboundEvent",
    "ChannelInboundRejected",
    "ChannelInboundResult",
    "ChannelInboundUncertain",
    "ChannelLegacyManaged",
    "ChannelNotFound",
    "ChannelOperationUncertain",
    "ChannelPlatform",
    "ChannelPlatformCapabilities",
    "ChannelPlatformSummary",
    "ChannelRequestConflict",
    "ChannelRevisionConflict",
    "ChannelRuntimeStatus",
    "ChannelSendResult",
    "ChannelSendStatus",
    "ChannelUnsupportedPlatform",
    "new_connection_id",
    "utc_now",
    "validate_allowed_senders",
    "validate_credentials",
    "validate_metadata",
]
