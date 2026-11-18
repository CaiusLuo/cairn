"""Run event buffering and durable metadata checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cairn.agent.models.messages import LLMResponse, LLMUsage
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.models import ConversationSource, RunEvent
from cairn.conversation.service import RunLedger
from cairn.conversation.store import DomainStore
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
    assert ledger.visible_text == "x" * 10_000


def test_follow_wakes_on_intermediate_event(tmp_path: Path) -> None:
    import httpx

    from cairn.agent.budget import RunBudget
    from cairn.server.bootstrap import runtime_app
    from cairn.server.client import RuntimeClient

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


def test_event_pages_and_cursor_cover_all_events(tmp_path: Path) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")
    conversation = store.create_conversation(
        workspace=tmp_path,
        source=ConversationSource.CLI,
        initial_prompt="pages",
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="pages",
        source=ConversationSource.CLI,
        request_id="pages-request",
    )
    for index in range(550):
        store.append_run_event(run.run_id, "agent_step", {"index": index})
    cursor = store.run_event_cursor(run.run_id)
    assert cursor == 550
    consumed: list[int] = []
    after = 0
    while True:
        page = store.list_run_events(run.run_id, after=after)
        if not page:
            break
        consumed.extend(event.sequence for event in page)
        after = page[-1].sequence
    assert consumed == list(range(1, 551))
    assert store.run_event_cursor(run.run_id) == consumed[-1]


def test_only_persisted_events_wake_follow(tmp_path: Path) -> None:
    store = DomainStore(tmp_path / "domain.sqlite3")
    conversation = store.create_conversation(
        workspace=tmp_path,
        source=ConversationSource.CLI,
        initial_prompt="wake",
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="wake",
        source=ConversationSource.CLI,
        request_id="wake-request",
    )
    wakes: list[bool] = []
    ledger = RunLedger(
        store, run, on_event=lambda _kind, persisted: wakes.append(persisted)
    )
    for _ in range(1000):
        ledger.record(ledger.task, "model_text_delta", {"text": "a"})
    assert wakes and all(flag is False for flag in wakes)
    ledger.record(ledger.task, "agent_step", {})
    assert wakes[-1] is True


def test_shared_runtime_timeline_correlates_and_persists_a_trace(
    tmp_path: Path,
) -> None:
    import httpx

    from cairn.agent.budget import RunBudget
    from cairn.server.bootstrap import runtime_app
    from cairn.server.client import RuntimeClient
    from tests.support.git import initialize_repository

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
                content="timeline complete",
                usage=LLMUsage(input_tokens=3, output_tokens=2),
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
            conversation = await api.create_conversation("timeline", source="cli")
            submitted = await api.send_conversation(
                conversation["conversation_id"],
                "timeline",
                source="cli",
                request_id="timeline-request",
            )
            stored = submitted
            for _ in range(200):
                stored = await api.get_run(submitted["run_id"])
                if stored["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert stored["status"] == "completed", stored
            events = (await api.run_events(submitted["run_id"]))["events"]
            by_kind = {event["kind"]: event for event in events}
            assert {"model_request", "model_response", "trace_finish"} <= set(by_kind)

            request = by_kind["model_request"]["data"]
            assert request["attempt"] == 1
            assert request["context_prepare_ms"] >= 0
            assert UUID(request["session_id"])
            assert UUID(request["turn_id"])
            assert request["run_id"] == submitted["run_id"]
            assert request["conversation_id"] == conversation["conversation_id"]
            assert by_kind["context_prepare"]["data"]["phase"] == "system"
            response = by_kind["model_response"]["data"]
            assert response["duration_ms"] >= 0
            assert response["input_tokens"] == 3
            assert response["output_tokens"] == 2
            committed = by_kind["turn_committed"]["data"]
            assert committed["persist_ms"] >= 0

            finish = by_kind["trace_finish"]["data"]
            assert finish["persisted"] is True
            trace_id = finish["trace_id"]
            trace_file = tmp_path / "state" / "traces" / f"{trace_id}.jsonl"
            assert trace_file.exists()
            assert stored["trace_id"] == trace_id
            assert stored["session_id"] == request["session_id"]
            assert stored["conversation_id"] == conversation["conversation_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("timeline_fails", [False, True])
def test_diagnostic_failures_do_not_abort_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeline_fails: bool
) -> None:
    import httpx

    from cairn.agent.budget import RunBudget
    from cairn.agent.tracing.tracer import Tracer
    from cairn.server.bootstrap import runtime_app
    from cairn.server.client import RuntimeClient
    from tests.support.git import initialize_repository

    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)

    from cairn.development.execution.timeline import DIAGNOSTIC_EVENTS

    if timeline_fails:
        append = DomainStore.append_run_event

        def broken_event_append(
            store: DomainStore,
            run_id: str,
            kind: str,
            data: dict[str, Any] | None = None,
        ) -> RunEvent:
            if kind in DIAGNOSTIC_EVENTS:
                raise OSError("diagnostic row unavailable")
            return append(store, run_id, kind, data)

        monkeypatch.setattr(DomainStore, "append_run_event", broken_event_append)

    class BrokenSink:
        def emit(self, span: Any) -> None:
            raise OSError("trace disk unavailable")

    class OneShot(OfflineDevelopmentModel):
        async def generate(
            self,
            messages: list[Any],
            tools: list[dict[str, Any]] | None = None,
        ) -> LLMResponse:
            return LLMResponse(content="completed despite broken trace")

    async def scenario() -> None:
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
            tracer=Tracer(BrokenSink()),
        )
        app.state.execution.llm_factory = lambda: OneShot()
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("broken trace", source="cli")
            submitted = await api.send_conversation(
                conversation["conversation_id"],
                "broken trace",
                source="cli",
                request_id="broken-trace-request",
            )
            stored = submitted
            for _ in range(200):
                stored = await api.get_run(submitted["run_id"])
                if stored["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert stored["status"] == "completed", stored
            assert stored["result"] == "completed despite broken trace"
            events = (await api.run_events(submitted["run_id"]))["events"]
            assert stored["trace_id"]
            assert any(event["kind"] == "turn_committed" for event in events)
            if timeline_fails:
                assert all(event["kind"] not in DIAGNOSTIC_EVENTS for event in events)
            else:
                finish = next(
                    event["data"] for event in events if event["kind"] == "trace_finish"
                )
                assert finish["persisted"] is False
                assert finish["error_code"] == "sink_failed"
                assert stored["trace_id"] == finish["trace_id"]

    asyncio.run(scenario())


def test_diagnostic_retention_preserves_lifecycle_and_event_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.conversation.store as store_module

    store = DomainStore(tmp_path / "domain.sqlite3")
    conversation = store.create_conversation(
        workspace=tmp_path, source=ConversationSource.CLI, initial_prompt="retention"
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="retention",
        source=ConversationSource.CLI,
        request_id="retention",
    )
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "utc_now", lambda: "2000-01-01T00:00:00+00:00")
        store.append_run_event(run.run_id, "turn_started")
        store.append_run_event(run.run_id, "trace_start")
    for kind in ("model_response", "model_response", "model_response", "trace_finish"):
        store.append_run_event(run.run_id, kind)
    store.prune_run_diagnostics(
        kinds=("trace_start", "model_response", "trace_finish"),
        before="2024-01-01T00:00:00+00:00",
        keep=2,
    )
    assert [row.sequence for row in store.list_run_events(run.run_id)] == [1, 4, 5, 6]
    assert store.run_event_cursor(run.run_id) == 6
    assert store.append_run_event(run.run_id, "turn_settled").sequence == 7
