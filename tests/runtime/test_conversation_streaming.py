"""Run event buffering and durable metadata checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cairn.core.domain import ConversationSource
from cairn.core.models import LLMResponse, LLMUsage
from cairn.infra.domain_store import DomainStore
from cairn.runtime.conversation_service import RunLedger
from cairn.runtime.offline_model import OfflineDevelopmentModel
from tests.support.git import initialize_repository


def test_stream_deltas_are_batched_into_bounded_events(tmp_path: Path) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")
    conversation = store.create_conversation(
        workspace=tmp_path,
        source=ConversationSource.CLI,
        initial_prompt="stream",
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="stream",
        source=ConversationSource.CLI,
        request_id="stream-request",
    )
    ledger = RunLedger(store, run)
    for _ in range(10_000):
        ledger.record(ledger.task, "model_text_delta", {"text": "x"})
    ledger.flush_preview()
    events = store.list_run_events(run.run_id)
    persisted = sum(1 for event in events if event.kind == "model_text_delta")
    assert persisted <= 2
    assert sum(len(event.data.get("text", "")) for event in events) == 10_000
    assert len(events) <= 2


def test_follow_wakes_on_intermediate_event(tmp_path: Path) -> None:
    import httpx

    from cairn.core.budget import RunBudget
    from cairn.infra.runtime_client import RuntimeClient
    from cairn.server import runtime_app

    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)

    class OneShot(OfflineDevelopmentModel):
        async def generate(
            self,
            messages: list[Any],
            tools: list[dict[str, Any]] | None = None,
        ) -> LLMResponse:
            return LLMResponse(
                content="follow complete",
                usage=LLMUsage(input_tokens=1, output_tokens=1),
            )

    async def scenario() -> None:
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: OneShot()
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("follow", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "follow",
                source="cli",
                request_id="follow-request",
            )
            events = await api.run_events(run["run_id"], after=0, wait=True)
            assert events["events"]
            assert events["cursor"] >= 1

    asyncio.run(scenario())
