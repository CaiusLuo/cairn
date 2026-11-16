"""Shared fakes for channel-core tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cairn.channels.adapter import (
    ChannelAdapterRegistry,
    FakeChannelAdapter,
    InboundSink,
)
from cairn.channels.manager import ChannelManager
from cairn.channels.models import (
    ALL_PLATFORMS,
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
    metadata: list[dict[str, object] | None] = field(default_factory=list)
    fail: bool = False
    fail_after_submit: bool = False
    conversations: dict[tuple[str, str, str, str], SimpleNamespace] = field(
        default_factory=dict
    )
    runs: dict[tuple[str, str], SimpleNamespace] = field(default_factory=dict)

    @staticmethod
    def _key(
        platform: str, connection_id: str, sender_id: str, conversation_binding: str
    ) -> tuple[str, str, str, str]:
        return (platform, connection_id, sender_id, conversation_binding)

    def ensure_channel_conversation(
        self,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
        initial_prompt: str,
    ) -> Any:
        key = self._key(platform, connection_id, sender_id, conversation_binding)
        if key not in self.conversations:
            self.conversations[key] = SimpleNamespace(
                conversation_id=f"conv-{connection_id}-{sender_id}-{conversation_binding}"
            )
        return self.conversations[key]

    def submit(
        self,
        conversation_id: str,
        prompt: str,
        source: ConversationSource,
        request_id: str,
        *,
        model_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Any:
        if self.fail:
            raise RuntimeError("intake failed with secret-value")
        key = (conversation_id, request_id)
        if key in self.runs:
            return self.runs[key]
        self.submissions.append((conversation_id, prompt, source, request_id))
        self.metadata.append(metadata)
        run = SimpleNamespace(run_id=f"run-{len(self.submissions)}")
        self.runs[key] = run
        if self.fail_after_submit:
            self.fail_after_submit = False
            raise RuntimeError("intake lost its response")
        return run

    def get_run(self, run_id: str) -> Any:
        return next(
            (run for run in self.runs.values() if run.run_id == run_id),
            None,
        )

    def get_channel_conversation(
        self,
        platform: str,
        connection_id: str,
        sender_id: str,
        conversation_binding: str,
    ) -> Any:
        return self.conversations.get(
            self._key(platform, connection_id, sender_id, conversation_binding)
        )

    def run_by_request(self, conversation_id: str, request_id: str) -> Any:
        return self.runs.get((conversation_id, request_id))


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
    registry = ChannelAdapterRegistry()
    for platform in ALL_PLATFORMS:
        registry.register(platform, factory)
    values: dict[str, Any] = {
        "store": store,
        "conversations": intake,
        "adapters": registry,
        "retry_delays": retry_delays,
    }
    if sleep is not None:
        values["sleep"] = sleep
    if secret_sink is not None:
        values["secret_sink"] = secret_sink
    manager = ChannelManager(**values)
    return manager, store, intake, factory


def inbound(
    connection_id: str,
    message_id: str,
    text: str = "hello",
    *,
    sender_id: str = "sender-1",
    platform: ChannelPlatform = ChannelPlatform.TELEGRAM,
    adapter_identity: str | None = "fake-bot",
) -> ChannelInboundEvent:
    return ChannelInboundEvent(
        platform=platform,
        connection_id=connection_id,
        external_message_id=message_id,
        sender_id=sender_id,
        conversation_binding="chat-1",
        text=text,
        adapter_identity=adapter_identity,
    )
