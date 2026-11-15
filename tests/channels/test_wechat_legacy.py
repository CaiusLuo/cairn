"""The legacy WeChat projection preserves the single host-owned session."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cairn.channels.models import (
    ChannelActualState,
    ChannelCredentialError,
    ChannelLegacyManaged,
    ChannelPlatform,
    ChannelSendStatus,
)
from cairn.channels.wechat import LEGACY_WECHAT_ID, wechat_legacy_adapter
from cairn.integrations.wechat.session import ProactiveSendOutcome
from cairn.shared.action import Action
from tests.channels.support import make_manager


class FakeWechatController:
    def __init__(self) -> None:
        self.status: dict[str, object] = {
            "account_saved": True,
            "running": False,
            "login_pending": False,
        }
        self.commands: list[str] = []
        self.deliveries: list[tuple[str, str, str]] = []
        self.outcome = ProactiveSendOutcome.SENT
        self.command_error: str | None = None

    def management_status(self) -> dict[str, object]:
        return dict(self.status)

    def account_id(self) -> str | None:
        return "bot-a"

    def recipient_id(self) -> str | None:
        return "owner"

    def secret_values(self) -> tuple[str, ...]:
        return ("wechat-secret-value",)

    async def command(self, args: str) -> Action:
        self.commands.append(args)
        if self.command_error is not None:
            raise RuntimeError(self.command_error)
        if args == "":
            self.status["running"] = True
        elif args == "stop":
            self.status["running"] = False
        return Action(message="ok")

    async def deliver_for_account_outcome(
        self,
        sender: str,
        text: str,
        account_id: str,
        *,
        context_token: str | None = None,
    ) -> ProactiveSendOutcome:
        self.deliveries.append((sender, text, account_id))
        return self.outcome


def test_legacy_adapter_projects_session_state_and_controls_it() -> None:
    async def scenario() -> None:
        controller = FakeWechatController()
        enabled = {"value": False}

        def enabled_value() -> bool:
            return enabled["value"]

        adapter = wechat_legacy_adapter(
            controller,  # type: ignore[arg-type]
            is_enabled=enabled_value,
            set_enabled=lambda value: enabled.__setitem__("value", value),
        )
        connection = adapter.connection()
        assert connection is not None
        assert connection.connection_id == LEGACY_WECHAT_ID
        assert connection.legacy is True
        assert connection.platform is ChannelPlatform.WECHAT
        assert connection.desired_state.value == "stopped"
        assert adapter.health().actual_state is ChannelActualState.STOPPED

        await adapter.start()
        assert controller.commands == [""]
        assert enabled_value() is True
        assert adapter.health().actual_state is ChannelActualState.RUNNING
        running_connection = adapter.connection()
        assert running_connection is not None
        assert running_connection.desired_state.value == "running"

        await adapter.stop()
        assert controller.commands == ["", "stop"]
        assert enabled_value() is False
        assert adapter.health().actual_state is ChannelActualState.STOPPED

        await adapter.reconnect()
        assert controller.commands == ["", "stop", "stop", ""]
        assert adapter.health().actual_state is ChannelActualState.RUNNING

    asyncio.run(scenario())


def test_legacy_adapter_refuses_start_without_a_saved_account() -> None:
    async def scenario() -> None:
        controller = FakeWechatController()
        controller.status["account_saved"] = False
        adapter = wechat_legacy_adapter(
            controller,  # type: ignore[arg-type]
            is_enabled=lambda: False,
            set_enabled=lambda _value: None,
        )
        assert adapter.connection() is None
        with pytest.raises(ChannelCredentialError):
            await adapter.start()
        assert controller.commands == []

    asyncio.run(scenario())


def test_legacy_adapter_maps_send_outcomes_without_replaying() -> None:
    async def scenario() -> None:
        controller = FakeWechatController()
        adapter = wechat_legacy_adapter(
            controller,  # type: ignore[arg-type]
            is_enabled=lambda: False,
            set_enabled=lambda _value: None,
        )
        accepted = await adapter.send("owner", "hello", "key-1")
        assert accepted.status is ChannelSendStatus.ACCEPTED
        assert controller.deliveries == [("owner", "hello", "bot-a")]

        controller.outcome = ProactiveSendOutcome.REJECTED
        rejected = await adapter.send("owner", "hello", "key-2")
        assert rejected.status is ChannelSendStatus.REJECTED
        assert rejected.error == "rejected"

        controller.outcome = ProactiveSendOutcome.UNKNOWN
        unknown = await adapter.send("owner", "hello", "key-3")
        assert unknown.status is ChannelSendStatus.UNKNOWN

    asyncio.run(scenario())


def test_manager_registers_legacy_secrets_and_redacts_control_errors(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        captured: list[tuple[str, ...]] = []
        manager, _, _, _ = make_manager(
            tmp_path / "channels", secret_sink=captured.append
        )
        controller = FakeWechatController()
        controller.command_error = "start failed with wechat-secret-value"
        manager.attach_legacy(
            wechat_legacy_adapter(
                controller,  # type: ignore[arg-type]
                is_enabled=lambda: False,
                set_enabled=lambda _value: None,
            )
        )
        assert captured == [("wechat-secret-value",)]
        view = await manager.start_connection(LEGACY_WECHAT_ID, "legacy-redacted")
        assert view.runtime.actual_state is ChannelActualState.STOPPED
        assert view.runtime.last_error is not None
        assert "wechat-secret-value" not in view.runtime.last_error
        await manager.close()

    asyncio.run(scenario())


def test_manager_unifies_legacy_wechat_and_keeps_it_host_managed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        manager, _, _, _ = make_manager(tmp_path / "channels")
        controller = FakeWechatController()
        enabled = {"value": False}
        manager.attach_legacy(
            wechat_legacy_adapter(
                controller,  # type: ignore[arg-type]
                is_enabled=lambda: enabled["value"],
                set_enabled=lambda value: enabled.__setitem__("value", value),
            )
        )
        summaries = manager.platform_summaries()
        assert (
            next(
                item for item in summaries if item.platform is ChannelPlatform.WECHAT
            ).display_state.value
            == "silent"
        )
        assert len(manager.list_connections()) == 1

        started = await manager.start_connection(LEGACY_WECHAT_ID, "legacy-start")
        assert started.runtime.actual_state is ChannelActualState.RUNNING
        replay = await manager.start_connection(LEGACY_WECHAT_ID, "legacy-start")
        assert replay.runtime.actual_state is ChannelActualState.RUNNING
        assert controller.commands == [""]

        with pytest.raises(ChannelLegacyManaged):
            await manager.update_connection(
                LEGACY_WECHAT_ID,
                expected_revision=1,
                request_id="legacy-update",
                changes={"display_name": "Renamed"},
            )
        with pytest.raises(ChannelLegacyManaged):
            await manager.delete_connection(
                LEGACY_WECHAT_ID,
                expected_revision=1,
                request_id="legacy-delete",
                confirmed=True,
            )
        await manager.close()

    asyncio.run(scenario())
