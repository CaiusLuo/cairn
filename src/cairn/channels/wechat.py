"""Legacy WeChat adapter around the existing single-session controller.

The controller, QR login, Conversation/Run path, Scheduler and daily review
delivery stay exactly where they are. This adapter only projects that state into
the channel API and forwards explicit start/stop/reconnect controls.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from cairn.channels.models import (
    ChannelActualState,
    ChannelAdapterError,
    ChannelAdapterHealth,
    ChannelConnection,
    ChannelCredentialError,
    ChannelDesiredState,
    ChannelPlatform,
    ChannelSendResult,
    ChannelSendStatus,
    utc_now,
)
from cairn.integrations.wechat.session import (
    ProactiveSendOutcome,
    WechatSessionController,
)
from cairn.shared.action import Action


class WechatController(Protocol):
    def management_status(self) -> dict[str, object]: ...

    def account_id(self) -> str | None: ...

    def recipient_id(self) -> str | None: ...

    def secret_values(self) -> tuple[str, ...]: ...

    async def command(self, args: str) -> Action: ...

    async def deliver_for_account_outcome(
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
    ) -> ProactiveSendOutcome: ...


LEGACY_WECHAT_ID = "legacy-wechat"


class WechatLegacyAdapter:
    """One host-owned WeChat session; a second instance is never created."""

    externally_managed = True

    def __init__(
        self,
        controller: WechatController,
        *,
        is_enabled: Callable[[], bool],
        set_enabled: Callable[[bool], None],
        connection_id: str = LEGACY_WECHAT_ID,
        created_at: str | None = None,
    ) -> None:
        if connection_id != LEGACY_WECHAT_ID:
            raise ValueError("WeChat is a single legacy connection")
        self.controller = controller
        self.connection_id = connection_id
        self.platform = ChannelPlatform.WECHAT
        self._is_enabled = is_enabled
        self._set_enabled = set_enabled
        self._created_at = created_at or utc_now()
        self._last_connected_at = (
            utc_now() if bool(controller.management_status().get("running")) else None
        )

    def connection(self) -> ChannelConnection | None:
        status = self.controller.management_status()
        running = bool(status.get("running"))
        pending = bool(status.get("login_pending"))
        saved = bool(status.get("account_saved"))
        configured = saved or pending or running or self._is_enabled()
        if not configured:
            return None
        desired = (
            ChannelDesiredState.RUNNING
            if self._is_enabled() or running or pending
            else ChannelDesiredState.STOPPED
        )
        return ChannelConnection(
            connection_id=self.connection_id,
            platform=self.platform,
            display_name="个人微信 Bot",
            enabled=saved or pending or running,
            desired_state=desired,
            credential_ref=None,
            owner_identity=None,
            metadata={"legacy": True, "adapter": "wechat_session"},
            revision=1,
            created_at=self._created_at,
            updated_at=self._created_at,
            legacy=True,
        )

    async def start(self) -> None:
        if not bool(self.controller.management_status().get("account_saved")):
            raise ChannelCredentialError("wechat_account_missing")
        action = await self.controller.command("")
        if action.tone != "info":
            raise ChannelAdapterError(action.message)
        self._set_enabled(True)
        self._last_connected_at = utc_now()

    async def stop(self) -> None:
        action = await self.controller.command("stop")
        if action.tone != "info":
            raise ChannelAdapterError(action.message)
        self._set_enabled(False)
        self._last_connected_at = None

    async def reconnect(self) -> None:
        if not bool(self.controller.management_status().get("account_saved")):
            raise ChannelCredentialError("wechat_account_missing")
        await self.stop()
        await self.start()

    def health(self) -> ChannelAdapterHealth:
        status = self.controller.management_status()
        running = bool(status.get("running"))
        pending = bool(status.get("login_pending"))
        if running:
            state = ChannelActualState.RUNNING
            last_connected = self._last_connected_at
        elif pending:
            state = ChannelActualState.CONNECTING
            last_connected = None
        else:
            state = ChannelActualState.STOPPED
            last_connected = None
        return ChannelAdapterHealth(
            actual_state=state,
            last_connected_at=last_connected,
            last_heartbeat_at=utc_now() if running else None,
            last_error=None,
        )

    def secret_values(self) -> tuple[str, ...]:
        """Legacy credentials stay host-owned but must still be filtered."""
        return tuple(value for value in self.controller.secret_values() if value)

    async def send(
        self, target: str, message: str, idempotency_key: str
    ) -> ChannelSendResult:
        account_id = self.controller.account_id()
        if not account_id:
            return ChannelSendResult(
                status=ChannelSendStatus.REJECTED, error="wechat_account_unavailable"
            )
        outcome = await self.controller.deliver_for_account_outcome(
            target, message, account_id
        )
        if outcome is ProactiveSendOutcome.SENT:
            return ChannelSendResult(status=ChannelSendStatus.ACCEPTED)
        if outcome is ProactiveSendOutcome.UNKNOWN:
            return ChannelSendResult(status=ChannelSendStatus.UNKNOWN)
        return ChannelSendResult(status=ChannelSendStatus.REJECTED, error=outcome.value)


def wechat_legacy_adapter(
    controller: WechatSessionController,
    *,
    is_enabled: Callable[[], bool],
    set_enabled: Callable[[bool], None],
) -> WechatLegacyAdapter:
    return WechatLegacyAdapter(
        controller, is_enabled=is_enabled, set_enabled=set_enabled
    )


__all__ = ["LEGACY_WECHAT_ID", "WechatController", "WechatLegacyAdapter"]
