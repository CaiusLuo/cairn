"""Shared fakes for channel-core tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cairn.channels.adapter import FakeChannelAdapter, InboundSink
from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ChannelConnection,
    ChannelInboundEvent,
    ChannelPlatform,
)
from cairn.channels.store import ChannelStore
from cairn.conversation.models import ConversationSource


@dataclass
class FakeIntake:
    submissions: list[tuple[str, str, ConversationSource, str]] = field(
        default_factory=list
    )
    fail: bool = False

    def ensure_channel_conversation(
        self,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
        initial_prompt: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            conversation_id=f"conv-{connection_id}-{sender_id}-{conversation_binding}"
        )

    def submit(
        self,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        *,
        model_id: str | None = None,
    ) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("intake failed with secret-value")
        self.submissions.append((conversation_id, prompt, source, request_id))
        return SimpleNamespace(run_id=f"run-{len(self.submissions)}")


class CapturingFactory:
    def __init__(self) -> None:
        self.adapters: dict[str, FakeChannelAdapter] = {}
        self.credentials: dict[str, dict[str, str] | None] = {}
        self.fail_starts: set[str] = set()
        self.raise_with_secret: str | None = None

    def __call__(
        self,
        connection: ChannelConnection,
        credentials: dict[str, str] | None,
        inbound: InboundSink,
    ) -> FakeChannelAdapter:
        adapter = FakeChannelAdapter(connection, inbound, credentials)
        if connection.connection_id in self.fail_starts:
            adapter.fail_start = True
        if self.raise_with_secret is not None:
            secret = self.raise_with_secret

            async def failing_start() -> None:
                raise RuntimeError(f"adapter rejected {secret}")

            adapter.start = failing_start  # type: ignore[method-assign]
        self.adapters[connection.connection_id] = adapter
        self.credentials[connection.connection_id] = credentials
        return adapter


def make_manager(
    root: Path,
    *,
    intake: FakeIntake | None = None,
    factory: CapturingFactory | None = None,
    retry_delays: tuple[float, ...] = (0,),
    sleep: Any = None,
    secret_sink: Any = None,
) -> tuple[ChannelManager, ChannelStore, FakeIntake, CapturingFactory]:
    store = ChannelStore(root)
    intake = intake or FakeIntake()
    factory = factory or CapturingFactory()
    values: dict[str, Any] = {
        "store": store,
        "conversations": intake,
        "adapter_factory": factory,
        "retry_delays": retry_delays,
    }
    if sleep is not None:
        values["sleep"] = sleep
    if secret_sink is not None:
        values["secret_sink"] = secret_sink
    manager = ChannelManager(**values)
    return manager, store, intake, factory


def inbound(
    connection_id: str, message_id: str, text: str = "hello"
) -> ChannelInboundEvent:
    return ChannelInboundEvent(
        platform=ChannelPlatform.TELEGRAM,
        connection_id=connection_id,
        external_message_id=message_id,
        sender_id="sender-1",
        conversation_binding="chat-1",
        text=text,
    )
